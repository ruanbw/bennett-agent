"""客户端回放历史 → pydantic-ai 消息序列的转换与校验。

服务端无状态、历史每轮由客户端全量回放，所以这里是唯一能拦住非法序列的地方；
`InvalidChatHistory` 的文案会原样回给用户，不能与通用的 `ValidationError` 混为一类。
"""

import json
import logging
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    UserPromptPart,
)

from app.schemas.chat import ChatMessage

logger = logging.getLogger(__name__)


class InvalidChatHistory(ValueError):
    """`convert_chat_history` 的校验失败。

    只有本异常的 message 可外泄：`ValidationError` 也是 `ValueError`，按后者分类会漏内部细节。
    """


def convert_chat_history(
    messages: list[ChatMessage],
    *,
    resuming: bool = False,
) -> tuple[str | None, list[ModelMessage]]:
    """把前端历史转成 PydanticAI 的 (prompt, message_history)；校验失败抛 `InvalidChatHistory`（文案可外泄）。

    `prompt` 只在续跑时为 `None` 而非 `""`：空串会产出一条 content 为空的 user 消息，Anthropic 系直接 400。
    工具调用必须与正文同放一个 `ModelResponse`，拆成两条会被判序列非法。
    """
    if not messages:
        raise InvalidChatHistory("消息列表不能为空")

    # 首条必须是 user：序列以 ModelRequest 开头且 request/response 交替；公开函数自己兜一道。
    if messages[0].role != "user":
        raise InvalidChatHistory("首条消息必须是 user 消息")

    # 严格交替：连续同角色在装配层会被合并/丢弃，模型看到的上下文与用户以为的不一致。
    for index in range(1, len(messages)):
        if messages[index].role == messages[index - 1].role:
            raise InvalidChatHistory(
                f"消息必须以 user 与 assistant 交替出现：第 {index} 条与第 {index + 1} 条都是 "
                f"{messages[index].role} 消息"
            )

    if resuming:
        # 续跑：末条是带工具调用的 assistant，没有新的用户提问；`None` 而非 `""` 见 docstring。
        current_prompt: str | None = None
        replay = messages
        last = messages[-1]
        if last.role != "assistant" or not last.tool_calls:
            raise InvalidChatHistory("续跑审批时最后一条必须是带工具调用的 assistant 消息")
    else:
        last_msg = messages[-1]
        if last_msg.role != "user" or not last_msg.content or not last_msg.content.strip():
            raise InvalidChatHistory("最后一条消息必须是内容非空的 user 消息")
        current_prompt = last_msg.content
        replay = messages[:-1]

    converted_history: list[ModelMessage] = []

    for msg in replay:
        if msg.role == "user":
            if msg.content:
                converted_history.append(ModelRequest(parts=[UserPromptPart(content=msg.content)]))
        else:
            parts: list[object] = []
            if msg.thinking:
                parts.append(ThinkingPart(content=msg.thinking))
            if msg.content:
                parts.append(TextPart(content=msg.content))
            # 入参解析失败时跳过这条调用而非整轮失败：历史由客户端回放，一条坏 JSON 不该让会话不可用。
            for call in msg.tool_calls:
                arguments = _parse_tool_args(call.args, call.tool_call_id)
                if arguments is None:
                    continue
                parts.append(
                    ToolCallPart(
                        tool_name=call.tool_name,
                        args=arguments,
                        tool_call_id=call.tool_call_id,
                    )
                )
            if parts:
                converted_history.append(ModelResponse(parts=parts))

    return current_prompt, converted_history


def _parse_tool_args(args_text: str, tool_call_id: str) -> dict[str, Any] | str | None:
    """解析客户端回放的工具入参；解析报错返回 `None`，调用方跳过这条调用。

    只记 WARNING 不抛：回放的历史可能是几天前存的，一条坏记录不该让整轮 500。
    """
    text = (args_text or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        logger.warning("工具调用入参不是合法 JSON，已跳过 (tool_call_id=%s)", tool_call_id)
        return None
    if isinstance(parsed, dict):
        return parsed
    # 非对象（数组/字符串/数字）原样交出：pydantic-ai 会当作未解析的 JSON 文本再校验。
    return text
