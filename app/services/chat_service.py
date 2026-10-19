"""对话流服务：历史转换、pydantic-ai 事件翻译、SSE 事件产出。

本模块只产出**类型化事件**（`app/schemas/events.py`），不再产出 `"data: ..."` 字符串。
序列化与 SSE 分帧是传输层的职责，见 `app/api/chat.py`。
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolCallPartDelta,
    UserPromptPart,
)

from app.schemas.chat import ChatMessage
from app.schemas.events import (
    ChatEvent,
    ClarificationRequest,
    ContentDelta,
    Done,
    FallbackUsed,
    StreamError,
    ThinkingDelta,
    ToolCall,
    ToolResult,
)
from app.services.agent_factory import build_agent, build_title_agent
from app.services.fallback_tracker import FallbackOccurrence, collect_fallbacks
from app.services.token_budget import overflow_message, resolve_context_budget
from app.services.upstream_errors import (
    is_retryable_for_user,
    is_retryable_upstream_error,
    upstream_error_message,
)

logger = logging.getLogger(__name__)

#: 意图确认工具名。服务端在这里把它翻译成 `ClarificationRequest` 事件，
#: 客户端不再需要知道这个字符串。
CLARIFICATION_TOOL_NAME = "ask_user_clarification"

MAX_TITLE_INPUT_CHARS = 500
MAX_TITLE_CHARS = 20
FALLBACK_TITLE_CHARS = 15
DEFAULT_TITLE = "新对话"

_TITLE_QUOTE_CHARS = "\"'“”‘’"

#: 单轮对话的**总**时长预算（秒）。上游超时是「空闲」语义（多久没收到数据），
#: 只约束单次等待；叠加 SDK 默认的 2 次重试与网关侧重试后，单轮仍可能被拖到十几分钟。
#: 这一道是总体上界：超预算即中止并给出可重试的提示。
TOTAL_STREAM_BUDGET_SECONDS = 600

#: 上游**瞬时**失败的重试预算。首次请求 + 1 次重试。
#: 只用于「换一次就可能成功」的失败（5xx、超时、连接类错误），4xx 与限流不重试：
#: 前者是请求本身的问题，后者再打一次只会加重上游负担。重试必须在**还没向客户端
#: 产出任何事件**时进行，否则正文会重复、带副作用的工具会执行两次。
MAX_STREAM_ATTEMPTS = 2
STREAM_RETRY_BACKOFF_SECONDS = 0.5

#: 上游 HTTP 状态码 → 可外泄的中文文案，以及「要不要重试 / 切备用模型」的判定，
#: 统一在 `app/services/upstream_errors.py`：备用模型装配（`agent_factory`）必须与
#: 对话流共用同一套判定，否则两处对「哪些错误可恢复」的理解会漂移。
STREAM_TIMEOUT_MESSAGE = "生成超时，请重试"

#: `allow_custom_input` 的「假值」写法。`bool("false")` 是 True——网关把 JSON 布尔
#: 值当字符串回传时，用户明明选了「不允许自定义」却会看到输入框。
_FALSY_TEXT_VALUES = frozenset({"", "0", "false", "no", "off"})

#: 已告警过的未识别事件类型。未识别类型一旦出现就是**高频**的（每个流事件一条），
#: 不去重会把日志刷爆，反而掩盖真正的问题。
_WARNED_UNKNOWN_EVENT_KEYS: set[str] = set()


class InvalidChatHistory(ValueError):
    """`convert_chat_history` 的校验失败。

    用专用类型而不是裸 `ValueError` 是必需的：pydantic 的 `ValidationError` 也是
    `ValueError` 的子类，而本函数内部要构造 `TextPart`/`ThinkingPart` 等 pydantic 模型。
    若按 `ValueError` 分类，一旦这些构造失败，内部校验细节会被当成「写给用户看的
    文案」原样推给客户端。本异常的 message 才是可以外泄的那一类。
    """


def _new_error_id() -> str:
    """生成可回查服务端日志的关联 ID。

    只把该 ID 回给客户端，内部异常原文（可能含上游 URL、provider 报错上下文乃至密钥）
    一律不外泄，仅进日志。
    """
    return uuid4().hex[:12]


def _warn_unknown_once(key: str, message: str, *args: Any) -> None:  # noqa: ANN401
    """对未识别的 pydantic-ai 事件/part 类型告警一次（每种 key 只告一次）。

    这类未识别分支的后果是**正文/思考增量被静默丢弃**：前端只看到回答卡住不动，
    生产日志级别是 INFO，DEBUG 级别的记录根本不会出现。按类型去重是为了让它可被
    发现，又不至于每个事件刷一行。
    """
    if key in _WARNED_UNKNOWN_EVENT_KEYS:
        return
    _WARNED_UNKNOWN_EVENT_KEYS.add(key)
    logger.warning(message, *args)


# =====================================================================
# 历史转换
# =====================================================================
def convert_chat_history(messages: list[ChatMessage]) -> tuple[str, list[ModelMessage]]:
    """将前端上传的历史对话转换为 PydanticAI 所需的 (prompt, message_history)

    校验失败时抛出 `InvalidChatHistory`，其文案是刻意写给用户看的，调用方可以原样返回。

    只处理 user/assistant：schema 已不允许 `system` 角色。此前那个分支会把调用方
    提供的文本变成 `SystemPromptPart` 插进历史，等于把系统提示词的控制权交给客户端。
    """
    if not messages:
        raise InvalidChatHistory("消息列表不能为空")

    # 首条必须是 user：pydantic-ai 的消息序列以 ModelRequest 开头、request/response
    # 交替，从 assistant 开始会装配出非法序列（模型侧报错或上下文错位）。schema 层
    # 也会拦一道，这里是服务层自己的兜底——`convert_chat_history` 是公开函数。
    if messages[0].role != "user":
        raise InvalidChatHistory("首条消息必须是 user 消息")

    # 取最后一条消息作为当前轮 prompt
    last_msg = messages[-1]
    if last_msg.role != "user" or not last_msg.content or not last_msg.content.strip():
        raise InvalidChatHistory("最后一条消息必须是内容非空的 user 消息")

    current_prompt = last_msg.content
    converted_history: list[ModelMessage] = []

    for msg in messages[:-1]:
        if msg.role == "user":
            if msg.content:
                converted_history.append(ModelRequest(parts=[UserPromptPart(content=msg.content)]))
        else:
            parts = []
            if msg.thinking:
                parts.append(ThinkingPart(content=msg.thinking))
            if msg.content:
                parts.append(TextPart(content=msg.content))
            if parts:
                converted_history.append(ModelResponse(parts=parts))

    return current_prompt, converted_history


# =====================================================================
# pydantic-ai 事件 → 聊天事件
#
# 用 isinstance 分派（R-2）而不是 type(event).__name__ 字符串比较：字符串比较在
# pydantic-ai 升级改名后表现为「静默无输出」，且没有任何地方会报错。pyproject 里
# 写的是 pydantic-ai>=2.31.1 无上界，升级随时发生。
# =====================================================================
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
    """解析 `allow_custom_input`；缺失时默认允许自定义输入。

    字符串要显式解析：`bool("false")` 是 True，会把「不允许自定义」翻成允许。
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() not in _FALSY_TEXT_VALUES
    if isinstance(value, bool):
        return value
    return bool(value)


def _clean_options(raw_options: Any) -> list[str]:  # noqa: ANN401
    """清洗候选项：strip、去重、过滤空串（顺序保持原样）。"""
    if not isinstance(raw_options, list):
        return []
    cleaned = [option.strip() for option in raw_options if isinstance(option, str)]
    return [option for option in dict.fromkeys(cleaned) if option]


def _clarification_from(args: Any, tool_call_id: str) -> ClarificationRequest | None:  # noqa: ANN401
    """从 `ask_user_clarification` 的原始入参解出提问。

    返回 `None` 表示本轮不该展示提问（原因已记日志）：入参坏掉，或清洗后不足 2 个
    有效选项。后者与 `ask_user_clarification` 工具自身的规则一致——工具按「至少 2 个
    不同的非空选项」拒绝，两边不一致时用户会先看到一个只有 0/1 个选项的弹窗。
    """
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            parsed = None
    else:
        parsed = args

    question = parsed.get("question") if isinstance(parsed, dict) else None
    if not isinstance(question, str) or not question.strip():
        logger.warning("意图确认工具入参无法解析，本轮不会展示提问 (tool_call_id=%s, args=%r)", tool_call_id, args)
        return None

    options = _clean_options(parsed.get("options"))
    if len(options) < 2:
        logger.warning(
            "意图确认选项不足 2 个有效值，本轮不会展示提问 (tool_call_id=%s, options=%r)",
            tool_call_id,
            parsed.get("options"),
        )
        return None

    return ClarificationRequest(
        tool_call_id=tool_call_id,
        question=question.strip(),
        options=options,
        allow_custom_input=_parse_allow_custom_input(parsed.get("allow_custom_input")),
    )


def translate_event(
    event: Any,  # noqa: ANN401
    seen_clarification_ids: set[str] | None = None,
) -> Iterator[ChatEvent]:
    """把一个 pydantic-ai 流事件翻译成 0..n 个聊天事件。

    不认识的事件会记 WARNING（每种类型只记一次，见 `_warn_unknown_once`）——这是
    pydantic-ai 升级后唯一能发现「渲染悄悄停摆」的地方，而这些事件的正文/思考增量
    会被静默丢弃。`ToolCallPartDelta`（工具入参增量）属于**有意**忽略：入参在
    `FunctionToolCallEvent` 到达时一次性给出即可，所以它仍然只记 DEBUG。

    `seen_clarification_ids` 是**单轮**的去重集合：同一轮里模型可能对同一次提问重复
    发出工具调用事件，后到的应被忽略，否则前端会弹出两个一样的提问框。传 `None`
    表示不做去重（直接单测 `translate_event` 时就是这个语义）。
    """
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
            # 有意忽略：工具调用在 FunctionToolCallEvent 里一次性给出（含完整入参），
            # 起始 part 只是它的空壳，转发会让前端先收到一个没有入参的调用。
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

    if isinstance(event, FunctionToolCallEvent):
        part = event.part
        tool_call_id = part.tool_call_id or ""
        tool_name = part.tool_name or ""

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

    if isinstance(event, FunctionToolResultEvent):
        # 注意用的是 part.content（工具结果本体），不是 event.content——
        # 后者是「结果之后追加给模型的额外内容」，语义不同。
        part = event.part
        tool_name = getattr(part, "tool_name", "") or ""

        # 意图确认工具的返回值是 agent 循环的内部细节（模型需要它才能继续），
        # 对用户没有意义。不转发它，前端就完全不需要知道这个工具名。
        if tool_name == CLARIFICATION_TOOL_NAME:
            logger.debug("不转发意图确认工具的结果事件 (tool_call_id=%s)", getattr(part, "tool_call_id", ""))
            return

        yield ToolResult(
            tool_call_id=getattr(part, "tool_call_id", "") or "",
            tool_name=tool_name,
            result=_stringify(part.content),
        )
        return

    _warn_unknown_once(
        f"event:{type(event).__name__}",
        "未处理的 pydantic-ai 流事件: %s（其正文/思考增量会被静默丢弃）",
        type(event).__name__,
    )


# =====================================================================
# 流式生成
# =====================================================================
class StreamTimeoutError(TimeoutError):
    """整轮对话超出 `TOTAL_STREAM_BUDGET_SECONDS`。"""


async def _iterate_with_total_budget(
    stream: AsyncIterator[Any],
    budget_seconds: float,
) -> AsyncIterator[Any]:
    """按**总时长**预算消费上游事件流；超预算抛 `StreamTimeoutError`。

    本环境是 Python 3.10，没有 `asyncio.timeout`（3.11 才有），这里用
    「deadline + wait_for 剩余预算」等价实现：每轮等待只允许到 deadline 为止，
    累加起来就是整轮的总预算。

    取消语义必须保持原样：外层（客户端断开）取消时 `CancelledError` 直接上抛，
    不能被翻译成超时或错误事件。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    iterator = stream.__aiter__()

    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise StreamTimeoutError(f"超出总时长预算 {budget_seconds}s")
        try:
            event = await asyncio.wait_for(iterator.__anext__(), timeout=remaining)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError as exc:
            raise StreamTimeoutError(f"超出总时长预算 {budget_seconds}s") from exc
        yield event


def _take_fallback_events(occurrences: list[FallbackOccurrence]) -> Iterator[FallbackUsed]:
    """把本轮收集到的切换记录按发生顺序转成事件，并清空收集器。"""
    while occurrences:
        occurrence = occurrences.pop(0)
        yield FallbackUsed(from_model=occurrence.primary_model, to_model=occurrence.fallback_model)


async def stream_chat_events(
    model_name: str,
    messages: list[ChatMessage],
    reasoning_effort: str | None = None,
) -> AsyncGenerator[ChatEvent, None]:
    """产出本轮对话的聊天事件流。

    以 `Done` 或 `StreamError` 收尾；调用方不需要自己判断「流断了没有」。
    整轮受 `TOTAL_STREAM_BUDGET_SECONDS` 约束，超预算同样以 `StreamError` 收尾。

    上下文预算预检（`app/services/token_budget.py`）排在最前面：历史是按字符上限
    全量回放上来的，而字符与 token 不是一个量纲——超限的请求以前会一路走到上游，
    换回一个 400，再被翻译成「模型不可用，请更换模型」。那既不是真实原因，
    也不是可执行的建议（400 不重试、也不切备用模型）。超限时直接以
    `code="context_overflow"` 的 `StreamError` 收尾，不发上游、不装配 agent。

    上游的**瞬时**失败（5xx / 超时 / 连接错误）会重试一次，但有一个硬前提：本轮
    还没有向客户端产出任何事件。一旦正文、思考或工具调用已经流出，重试就会让用户
    看到重复内容，并让带副作用的工具执行两次——这种情况下直接以 `StreamError` 收尾。
    重试的等待时间也计入总预算，不会把总时长上限拖长。

    装配层配置了备用模型且真的发生了切换时，会先产出一个 `FallbackUsed` 事件：
    用户需要知道本轮回答来自另一个模型，否则排障时既不知道是谁答的，也容易把备用
    模型的能力差异误判成「这个模型变差了」。
    """
    seen_clarification_ids: set[str] = set()

    # 预检排在历史转换与装配之前：判定已经不可能通过的请求没有必要再走后面两步。
    budget = await resolve_context_budget(model_name, messages)
    if budget is not None and budget.over_limit:
        logger.warning(
            "上下文超出模型预算，已拒绝本轮 (model=%s, used=%d, limit=%d, window=%d, messages=%d)",
            model_name,
            budget.used_tokens,
            budget.limit_tokens,
            budget.context_window,
            len(messages),
        )
        yield StreamError(
            message=overflow_message(budget),
            retryable=False,
            code="context_overflow",
        )
        return

    try:
        current_prompt, history = convert_chat_history(messages)
    except InvalidChatHistory as exc:
        # 刻意写给用户看的校验文案，可以原样返回。请求本身不合法，重试结果一样。
        yield StreamError(message=str(exc), retryable=False)
        return
    except Exception:
        error_id = _new_error_id()
        logger.exception("对话历史转换失败 (error_id=%s)", error_id)
        # 内部错误而非用户输入问题：可能是瞬时故障，允许用户重试
        yield StreamError(message=f"请求处理失败，请重试（错误编号 {error_id}）", retryable=True)
        return

    try:
        agent = build_agent(model_name, reasoning_effort=reasoning_effort)
    except Exception:
        error_id = _new_error_id()
        logger.exception("模型初始化失败 (error_id=%s, model=%s)", error_id, model_name)
        yield StreamError(message=f"模型初始化失败，请重试或切换模型（错误编号 {error_id}）", retryable=True)
        return

    loop = asyncio.get_running_loop()
    deadline = loop.time() + TOTAL_STREAM_BUDGET_SECONDS
    attempt = 0

    # 收集器必须覆盖整个重试循环：切换发生在 agent 内部，却要在本层翻译成事件。
    with collect_fallbacks() as fallbacks:
        while True:
            attempt += 1
            # 是否已经向客户端产出过事件。只在这个标志为 False 时允许重试（见 docstring）。
            emitted = False
            # 上一次尝试可能记录了切换却没走到事件阶段（随后整体失败）。重试成功后
            # 用户看到的是这一轮的结果，不能把上一轮的切换误报成「本轮已切换」。
            fallbacks.clear()

            try:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise StreamTimeoutError(f"超出总时长预算 {TOTAL_STREAM_BUDGET_SECONDS}s")

                async with agent.run_stream_events(current_prompt, message_history=history) as stream:
                    async for event in _iterate_with_total_budget(stream, remaining):
                        # 切换发生在流打开阶段，先于任何模型事件：在第一条事件之前告诉用户。
                        for fallback in _take_fallback_events(fallbacks):
                            emitted = True
                            yield fallback
                        for chat_event in translate_event(event, seen_clarification_ids):
                            emitted = True
                            yield chat_event
                break
            except asyncio.CancelledError:
                # 客户端断开 / 任务取消：必须继续上抛，交给传输层收尾。若在这里产出 StreamError，
                # 等于往一条已经断开的连接里再写一帧，还会把「用户取消」记成生成失败。
                raise
            except StreamTimeoutError:
                error_id = _new_error_id()
                logger.warning(
                    "流式生成超出总时长预算 %ss，已中止 (error_id=%s, model=%s)",
                    TOTAL_STREAM_BUDGET_SECONDS,
                    error_id,
                    model_name,
                )
                yield StreamError(message=STREAM_TIMEOUT_MESSAGE, retryable=True)
                return
            except Exception as exc:
                if not emitted and attempt < MAX_STREAM_ATTEMPTS and is_retryable_upstream_error(exc):
                    logger.warning(
                        "上游瞬时失败，重试本轮生成 (第 %d/%d 次, model=%s)",
                        attempt + 1,
                        MAX_STREAM_ATTEMPTS,
                        model_name,
                        exc_info=True,
                    )
                    # 退避也受总预算约束：睡过头后下一轮的 remaining<=0 会按超时收尾。
                    await asyncio.sleep(STREAM_RETRY_BACKOFF_SECONDS)
                    continue

                error_id = _new_error_id()
                logger.exception("流式生成异常 (error_id=%s, model=%s)", error_id, model_name)
                yield StreamError(
                    message=f"{upstream_error_message(exc)}（错误编号 {error_id}）",
                    retryable=is_retryable_for_user(exc),
                )
                return

    yield Done()


async def generate_title_from_prompt(model_name: str, prompt: str) -> str:
    """根据用户输入的提示词意向，利用 LLM 快速生成简短概括的会话标题。

    失败时回退到「首行截断」——标题只是装饰，不该让用户看到报错。
    """
    try:
        agent = build_title_agent(model_name)
        result = await agent.run(f"请为以下内容生成简短标题：\n{prompt[:MAX_TITLE_INPUT_CHARS]}")
        title = result.output.strip().strip(_TITLE_QUOTE_CHARS)
        return title[:MAX_TITLE_CHARS] if title else DEFAULT_TITLE
    except Exception:
        logger.warning("生成会话标题失败，回退到截断首行 (model=%s)", model_name, exc_info=True)
        cleaned = prompt.strip().split("\n")[0][:FALLBACK_TITLE_CHARS]
        return cleaned or DEFAULT_TITLE
