"""SSE 聊天事件的类型化契约。

这是 R-1 的核心：事件从「带 `data:` 前缀的字符串」变成有判别键的类型。

改动前同一套分帧知识存在 4 份副本——

1. `chat_service` 产出 `"data: {json}\\n\\n"` 字符串；
2. `api/chat.py::_collect_chat_response` 把 `"data:"` 剥掉，取回自己需要的 JSON；
3. `api/chat.py::event_generator` 再剥一次，交给 `EventSourceResponse` 又加回去；
4. `frontend/src/lib/chat-runs.js` 第四次解析同一套分帧。

第 2、3 步是纯仪式：往返后字节完全一致。现在服务端只产类型对象，且只在
`app/api/chat.py` 一处序列化；客户端拿到的是有 `type` 判别键的结构，而不是
需要自己剥前缀的字符串。

序列化统一用 `model_dump_json()`（紧凑分隔符）。它与原先的
`json.dumps(..., ensure_ascii=False)` 语义等价，唯一差别是 `:` 后的空格；
两者都会把裸换行转义成 `\\n`，因此 SSE 分帧安全。
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field


class ContentDelta(BaseModel):
    """模型正文增量。"""

    type: Literal["content"] = "content"
    delta: str


class ThinkingDelta(BaseModel):
    """思考过程增量。"""

    type: Literal["thinking"] = "thinking"
    delta: str


class ToolCall(BaseModel):
    """工具开始调用。

    `args` 保持为字符串：前端原样展示，不做二次解析。格式化交给展示层。
    """

    type: Literal["tool_call"] = "tool_call"
    tool_call_id: str
    tool_name: str
    args: str


class ToolResult(BaseModel):
    """工具调用返回。"""

    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    tool_name: str
    result: str


class ClarificationRequest(BaseModel):
    """模型发起意图确认提问。

    R-6 的落点：提问是一等公民事件，字段已在服务端解析完毕。改动前前端要靠
    `event.tool_name === 'ask_user_clarification'` 比对字符串、再自己
    `JSON.parse(event.args)` 才能拿到提问——那是把一个协议细节漏进了展示层。
    """

    type: Literal["clarification"] = "clarification"
    tool_call_id: str
    question: str
    options: list[str]
    allow_custom_input: bool = True


class Done(BaseModel):
    """本轮生成正常结束。"""

    type: Literal["done"] = "done"


class StreamError(BaseModel):
    """本轮失败。

    `message` 只允许放可以外泄的文案：校验类错误可以给原文，内部异常只能给关联 ID。
    见 `chat_service.StreamFailure` 与 CODE_REVIEW.md C-4。
    """

    type: Literal["error"] = "error"
    message: str


#: SSE 事件载荷。判别键是 `type`，客户端可以直接按它 switch。
ChatEvent = Annotated[
    ContentDelta | ThinkingDelta | ToolCall | ToolResult | ClarificationRequest | Done | StreamError,
    Field(discriminator="type"),
]

__all__ = [
    "ChatEvent",
    "ClarificationRequest",
    "ContentDelta",
    "Done",
    "StreamError",
    "ThinkingDelta",
    "ToolCall",
    "ToolResult",
]
