"""对话流服务：历史转换、pydantic-ai 事件翻译、SSE 事件产出。

本模块只产出**类型化事件**（`app/schemas/events.py`），不再产出 `"data: ..."` 字符串。
序列化与 SSE 分帧是传输层的职责，见 `app/api/chat.py`。
"""

import json
import logging
from collections.abc import AsyncGenerator, Iterator
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
    UserPromptPart,
)

from app.schemas.chat import ChatMessage
from app.schemas.events import (
    ChatEvent,
    ClarificationRequest,
    ContentDelta,
    Done,
    StreamError,
    ThinkingDelta,
    ToolCall,
    ToolResult,
)
from app.services.agent_factory import build_agent, build_title_agent

logger = logging.getLogger(__name__)

#: 意图确认工具名。服务端在这里把它翻译成 `ClarificationRequest` 事件，
#: 客户端不再需要知道这个字符串。
CLARIFICATION_TOOL_NAME = "ask_user_clarification"

MAX_TITLE_INPUT_CHARS = 500
MAX_TITLE_CHARS = 20
FALLBACK_TITLE_CHARS = 15
DEFAULT_TITLE = "新对话"

_TITLE_QUOTE_CHARS = "\"'“”‘’"


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


def _clarification_from(args: Any, tool_call_id: str) -> ClarificationRequest | None:  # noqa: ANN401
    """从 `ask_user_clarification` 的原始入参解出提问；入参不合法时返回 None。"""
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            return None
    else:
        parsed = args

    if not isinstance(parsed, dict):
        return None

    question = parsed.get("question")
    if not isinstance(question, str) or not question.strip():
        return None

    raw_options = parsed.get("options")
    options = [opt for opt in raw_options if isinstance(opt, str)] if isinstance(raw_options, list) else []

    raw_allow = parsed.get("allow_custom_input")
    return ClarificationRequest(
        tool_call_id=tool_call_id,
        question=question.strip(),
        options=options,
        allow_custom_input=True if raw_allow is None else bool(raw_allow),
    )


def translate_event(event: Any) -> Iterator[ChatEvent]:  # noqa: ANN401
    """把一个 pydantic-ai 流事件翻译成 0..n 个聊天事件。

    不认识的事件只记 DEBUG 日志——这是 pydantic-ai 升级后唯一能发现「渲染悄悄
    停摆」的地方。`ToolCallPartDelta`（工具入参增量）属于有意忽略：入参在
    `FunctionToolCallEvent` 到达时一次性给出即可。
    """
    if isinstance(event, PartStartEvent):
        part = event.part
        if isinstance(part, TextPart) and part.content:
            yield ContentDelta(delta=part.content)
        elif isinstance(part, ThinkingPart) and part.content:
            yield ThinkingDelta(delta=part.content)
        else:
            logger.debug("忽略的起始 part: %s", type(part).__name__)
        return

    if isinstance(event, PartDeltaEvent):
        delta = event.delta
        if isinstance(delta, TextPartDelta):
            if delta.content_delta:
                yield ContentDelta(delta=delta.content_delta)
        elif isinstance(delta, ThinkingPartDelta):
            if delta.content_delta:
                yield ThinkingDelta(delta=delta.content_delta)
        elif not isinstance(delta, type(None)):
            logger.debug("忽略的 part 增量: %s", type(delta).__name__)
        return

    if isinstance(event, FunctionToolCallEvent):
        part = event.part
        tool_call_id = part.tool_call_id or ""
        tool_name = part.tool_name or ""

        if tool_name == CLARIFICATION_TOOL_NAME:
            request = _clarification_from(part.args, tool_call_id)
            if request is not None:
                yield request
            else:
                logger.warning(
                    "意图确认工具入参无法解析，本轮不会展示提问 (tool_call_id=%s, args=%r)",
                    tool_call_id,
                    part.args,
                )
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

    logger.debug("未处理的 pydantic-ai 流事件: %s", type(event).__name__)


# =====================================================================
# 流式生成
# =====================================================================
async def stream_chat_events(
    model_name: str,
    messages: list[ChatMessage],
    reasoning_effort: str | None = None,
) -> AsyncGenerator[ChatEvent, None]:
    """产出本轮对话的聊天事件流。

    以 `Done` 或 `StreamError` 收尾；调用方不需要自己判断「流断了没有」。
    """
    try:
        current_prompt, history = convert_chat_history(messages)
    except InvalidChatHistory as exc:
        # 刻意写给用户看的校验文案，可以原样返回
        yield StreamError(message=str(exc))
        return
    except Exception:
        error_id = _new_error_id()
        logger.exception("对话历史转换失败 (error_id=%s)", error_id)
        yield StreamError(message=f"请求处理失败，请重试（错误编号 {error_id}）")
        return

    try:
        agent = build_agent(model_name, reasoning_effort=reasoning_effort)
    except Exception:
        error_id = _new_error_id()
        logger.exception("模型初始化失败 (error_id=%s, model=%s)", error_id, model_name)
        yield StreamError(message=f"模型初始化失败，请重试或切换模型（错误编号 {error_id}）")
        return

    try:
        async with agent.run_stream_events(current_prompt, message_history=history) as stream:
            async for event in stream:
                for chat_event in translate_event(event):
                    yield chat_event
    except Exception:
        error_id = _new_error_id()
        logger.exception("流式生成异常 (error_id=%s, model=%s)", error_id, model_name)
        yield StreamError(message=f"生成异常，请重试（错误编号 {error_id}）")
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
