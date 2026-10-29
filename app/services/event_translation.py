"""pydantic-ai 流事件 → 聊天事件（`app/schemas/events.py`）的翻译层。

用 isinstance 分派而非 `type(event).__name__` 比较：后者在 pydantic-ai 升级改名后表现为静默无输出。
"""

import json
import logging
from collections.abc import Iterator
from typing import Any

from pydantic_ai import DeferredToolRequestsEvent
from pydantic_ai.messages import (
    FinalResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallEvent,
    ToolCallPart,
    ToolCallPartDelta,
    ToolResultEvent,
)
from pydantic_ai.run import AgentRunResultEvent

from app.runtime.clarification import normalize_clarification
from app.schemas.events import (
    ChatEvent,
    ClarificationRequest,
    ContentDelta,
    ThinkingDelta,
    ToolCall,
    ToolResult,
)
from app.services.metrics import TOOL_EXECUTIONS_TOTAL, metrics

logger = logging.getLogger(__name__)

#: 意图确认工具名，由本层翻译成 `ClarificationRequest`，客户端无需知道这个字符串。
CLARIFICATION_TOOL_NAME = "ask_user_clarification"

#: `allow_custom_input` 的假值写法。`bool("false")` 是 True——网关把 JSON 布尔值当字符串
#: 回传时，用户选了「不允许自定义」却会看到输入框。
_FALSY_TEXT_VALUES = frozenset({"", "0", "false", "no", "off"})

#: 已告警过的未识别事件类型：未识别类型一旦出现就是高频的（每个流事件一条），
#: 不去重会刷爆日志、反而掩盖真正的问题。
_WARNED_UNKNOWN_EVENT_KEYS: set[str] = set()


def _warn_unknown_once(key: str, message: str, *args: Any) -> None:  # noqa: ANN401
    """对未识别的 pydantic-ai 事件/part 类型告警一次（每种 key 只告一次）。

    这些分支的后果是正文/思考增量被静默丢弃，而生产日志级别是 INFO，只能靠 WARNING 发现。
    """
    if key in _WARNED_UNKNOWN_EVENT_KEYS:
        return
    _WARNED_UNKNOWN_EVENT_KEYS.add(key)
    logger.warning(message, *args)


def _stringify(value: Any) -> str:  # noqa: ANN401
    """把工具返回的任意内容压成字符串，失败时降级而不是抛错。"""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


_TOOL_OUTCOMES = frozenset({"success", "failed", "denied", "interrupted"})


def _tool_outcome(part: Any) -> str:  # noqa: ANN401
    """取工具结果的结构化 outcome，供前端区分成功与失败。

    用 outcome 而非让前端猜结果字符串；`RetryPromptPart` 无 outcome，本身就是参数校验失败，按 failed 处理。
    """
    outcome = getattr(part, "outcome", None)
    if outcome in _TOOL_OUTCOMES:
        return outcome
    if isinstance(part, RetryPromptPart):
        return "failed"
    return "success"


def _serialize_tool_args(args: Any) -> str:  # noqa: ANN401
    """工具入参保持字符串形态：前端原样展示，格式化交给展示层。"""
    if isinstance(args, str):
        return args
    if args is None:
        return ""
    try:
        return json.dumps(args, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(args)


def _parse_allow_custom_input(value: Any) -> bool:  # noqa: ANN401
    """解析 `allow_custom_input`，缺失时默认允许；字符串要显式解析，`bool("false")` 是 True。"""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in _FALSY_TEXT_VALUES
    if isinstance(value, bool):
        return value
    return bool(value)


def _clarification_from(args: Any, tool_call_id: str) -> ClarificationRequest | None:  # noqa: ANN401
    """从 `ask_user_clarification` 的原始入参解出提问；返回 `None` 表示本轮不展示提问。

    必须复用工具的 `normalize_clarification`：本层看到的是工具开始执行的事件，等不到它的拒绝结果。
    """
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            parsed = None
    else:
        parsed = args

    if not isinstance(parsed, dict):
        logger.warning("意图确认工具入参无法解析，本轮不会展示提问 (tool_call_id=%s, args=%r)", tool_call_id, args)
        return None

    normalized = normalize_clarification(parsed.get("question"), parsed.get("options"))
    if isinstance(normalized, str):
        # 工具文案已写明哪个参数越界，直接用它记日志，不必再对照两份规则。
        logger.warning(
            "意图确认入参未通过校验，本轮不会展示提问 (tool_call_id=%s, reason=%s)",
            tool_call_id,
            normalized,
        )
        return None

    return ClarificationRequest(
        tool_call_id=tool_call_id,
        question=normalized.question,
        options=normalized.options,
        allow_custom_input=_parse_allow_custom_input(parsed.get("allow_custom_input")),
    )


def translate_event(
    event: Any,  # noqa: ANN401
    seen_clarification_ids: set[str] | None = None,
) -> Iterator[ChatEvent]:
    """把一个 pydantic-ai 流事件翻译成 0..n 个聊天事件；未识别类型告警一次。

    `seen_clarification_ids` 是单轮去重集合（`None` 表示不去重）：同一轮的重复提问会让前端弹两个框。
    终态 `AgentRunResultEvent` 有意忽略：真实用量要配模型窗口，那是 `stream_chat_events` 的活。
    """
    if isinstance(event, AgentRunResultEvent):
        logger.debug("忽略的终态事件: %s（真实用量由 stream_chat_events 单独产出）", type(event).__name__)
        return

    if isinstance(event, PartStartEvent):
        part = event.part
        if isinstance(part, TextPart):
            if part.content:
                yield ContentDelta(delta=part.content)
            return
        if isinstance(part, ThinkingPart):
            if part.content:
                yield ThinkingDelta(delta=part.content)
            return
        if isinstance(part, ToolCallPart):
            # 有意忽略：调用在 `ToolCallEvent` 里带全入参一次性给出，起始 part 只是它的空壳。
            logger.debug("忽略的起始 part: %s", type(part).__name__)
            return
        _warn_unknown_once(
            f"part-start:{type(part).__name__}",
            "未处理的 pydantic-ai 起始 part: %s（其正文/思考增量会被静默丢弃）",
            type(part).__name__,
        )
        return

    if isinstance(event, PartDeltaEvent):
        delta = event.delta
        if isinstance(delta, TextPartDelta):
            if delta.content_delta:
                yield ContentDelta(delta=delta.content_delta)
            return
        if isinstance(delta, ThinkingPartDelta):
            if delta.content_delta:
                yield ThinkingDelta(delta=delta.content_delta)
            return
        if isinstance(delta, ToolCallPartDelta):
            logger.debug("忽略的 part 增量: %s", type(delta).__name__)
            return
        if delta is not None:
            _warn_unknown_once(
                f"part-delta:{type(delta).__name__}",
                "未处理的 pydantic-ai part 增量: %s（其正文/思考增量会被静默丢弃）",
                type(delta).__name__,
            )
        return

    # 用基类 `ToolCallEvent` 而非子类 `FunctionToolCallEvent`：同级还有 `OutputToolCallEvent`，
    # 用子类判断会让它落进兜底告警，工具调用整体消失。
    if isinstance(event, ToolCallEvent):
        part = event.part
        tool_call_id = getattr(part, "tool_call_id", "") or ""
        tool_name = getattr(part, "tool_name", "") or ""

        if tool_name == CLARIFICATION_TOOL_NAME:
            if tool_call_id and seen_clarification_ids is not None and tool_call_id in seen_clarification_ids:
                logger.debug("同一轮的重复意图确认，已忽略后到者 (tool_call_id=%s)", tool_call_id)
                return

            request = _clarification_from(part.args, tool_call_id)
            if request is None:
                return

            if tool_call_id and seen_clarification_ids is not None:
                seen_clarification_ids.add(tool_call_id)
            yield request
            return

        yield ToolCall(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            args=_serialize_tool_args(part.args),
        )
        return

    if isinstance(event, ToolResultEvent):
        # 用 part.content（结果本体），不是 event.content（结果之后追加给模型的额外内容）。
        part = event.part
        tool_name = getattr(part, "tool_name", "") or ""

        # 意图确认的返回是 agent 循环的内部细节（模型靠它继续），前端不需要知道这个工具名。
        if tool_name == CLARIFICATION_TOOL_NAME:
            logger.debug("不转发意图确认工具的结果事件 (tool_call_id=%s)", getattr(part, "tool_call_id", ""))
            return

        # 成功记在这里、失败在 `guard.on_tool_execute_error` 里记，两边同一组 `tool`+`outcome` 标签；
        # 失败率 = failed / (success + failed)，denied / interrupted 是审批与中断，不算故障。
        metrics.increment(
            TOOL_EXECUTIONS_TOTAL,
            tool=tool_name,
            outcome=_tool_outcome(part),
        )

        yield ToolResult(
            tool_call_id=getattr(part, "tool_call_id", "") or "",
            tool_name=tool_name,
            result=_stringify(part.content),
            outcome=_tool_outcome(part),
        )
        return

    if isinstance(event, DeferredToolRequestsEvent):
        # 审批拦下的调用原样交给 `stream_chat_events` 翻译：工具说明与风险等级由 `approval_flow` 查 catalog。
        logger.debug("审批拦下 %d 个工具调用", len(event.requests.approvals))
        return

    # `PartEndEvent` / `FinalResultEvent` 是每次响应的固定成分，内容已由增量发过；
    # 归入有意忽略，否则兜底告警会在 100% 的正常流量上打 WARNING，把唯一的信号淹掉。
    if isinstance(event, (PartEndEvent, FinalResultEvent)):
        logger.debug("忽略的流事件: %s", type(event).__name__)
        return

    _warn_unknown_once(
        f"event:{type(event).__name__}",
        "未处理的 pydantic-ai 流事件: %s（其正文/思考增量会被静默丢弃）",
        type(event).__name__,
    )
