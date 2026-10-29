"""SSE 聊天事件的类型化契约：判别键是 `type`，服务端只在 `app/api/chat.py` 一处序列化。
统一用 `model_dump_json()`（紧凑分隔符）：它会把裸换行转义成 `\n`，SSE 分帧才安全。
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
    """工具开始调用；`args` 保持字符串，前端原样展示、不做二次解析。"""

    type: Literal["tool_call"] = "tool_call"
    tool_call_id: str
    tool_name: str
    args: str


class ToolResult(BaseModel):
    """工具调用返回；`outcome` 区分成功 / 隔离失败 / 权限拒绝 / 中断。
    前端据 `outcome` 显示失败状态，不能把隔离后的错误文案当成一次成功调用。
    """

    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    tool_name: str
    result: str
    outcome: Literal["success", "failed", "denied", "interrupted"] = "success"


class ClarificationRequest(BaseModel):
    """模型发起意图确认提问：字段已在服务端解析完毕，前端不必比对 tool_name 再自己 parse args。"""

    type: Literal["clarification"] = "clarification"
    tool_call_id: str
    question: str
    options: list[str]
    allow_custom_input: bool = True


class FallbackUsed(BaseModel):
    """主模型失败、本轮已自动切到备用模型（静默切换会让能力差异被误判成模型变差）。
    只带模型 id 与原因类别短语：上游错误原文可能含 URL 与凭据上下文，只进日志。
    """

    type: Literal["fallback"] = "fallback"
    from_model: str
    to_model: str
    #: 切换原因类别（「上游限流」「上游 503」「请求超时」「连接失败」…）；
    #: 老服务端不带这个字段，前端按「未说明的原因」处理。
    reason: str = ""


class Done(BaseModel):
    """本轮生成正常结束。"""

    type: Literal["done"] = "done"


class UsageTiming(BaseModel):
    """本轮计时的实测明细（服务端量的），字段口径与 `app/services/usage.py::TurnTimer` 逐条对应。
    七个字段是同一个块：没量到就整块缺席，前端绝不补 0（`0 tok/s` 会被读成模型卡住）；
    `tool_ms` 无工具时是真实的 0，`ttft_ms` / `decode_ms` / `decode_tokens` 只取真实解码窗口、成对缺失。
    """

    steps: int
    turn_ms: int
    llm_ms: int
    tool_ms: int
    ttft_ms: int | None = None
    decode_ms: int | None = None
    decode_tokens: int | None = None


class Usage(BaseModel):
    """本轮的 token 用量（来自上游响应，不是估算），把口径写进字段名。
    `context_tokens` 是最后一次请求的 prompt token（不是累加）；`context_limit_tokens` 才是后端判超限的上限，
    与 `context_window` 不同；`cache_read_tokens` 是 `context_tokens` 的子集而非额外开销；上游没回就不发事件。
    """

    type: Literal["usage"] = "usage"
    context_tokens: int
    context_window: int | None = None
    context_limit_tokens: int | None = None
    output_tokens: int = 0
    cache_read_tokens: int = 0
    #: 本轮计时明细；`None` 表示服务端没量到（老服务端或流式链路拿不到分片时序），此时不显示也不补 0。
    #: 口径见 `UsageTiming` 的 docstring。
    timing: UsageTiming | None = None


class ContextCompacted(BaseModel):
    """本轮开始前做过上下文压缩（折叠早期消息或截断超长消息）——它改了模型看到的上下文，必须说。
    只给数字与策略，不下发摘要正文：正文可能含早期对话的敏感内容，而客户端会落库并同步到其他设备。
    """

    type: Literal["compaction"] = "compaction"
    #: `truncate` = 只截断了超长消息；`summarize` = 折叠了早期轮次。
    strategy: Literal["truncate", "summarize"]
    #: 被折叠成摘要的消息条数（`truncate` 时为 0）。
    dropped_messages: int = 0
    #: 被截断了中间部分的消息条数（`summarize` 时可能同时发生）。
    truncated_messages: int = 0
    #: 压缩前后的估算用量与上限，前端可据此说明「省了多少」。
    used_tokens_before: int
    used_tokens_after: int
    limit_tokens: int


class ApprovalToolCall(BaseModel):
    """一条待审批的工具调用（形状与 `ReplayedToolCall` 对齐，客户端可直接回放）。"""

    tool_call_id: str
    tool_name: str
    #: 调用入参（JSON 文本），供前端展示
    args: str
    #: 工具说明（取自工具自身的 description），帮用户理解「这是要做什么」
    description: str = ""
    #: 风险等级（`read_only` / `safe_write` / `dangerous` …）
    risk_level: str = ""


class ApprovalRequiredEvent(BaseModel):
    """本轮有工具调用需要用户批准，尚未执行（本轮到此结束，客户端回放后再续跑）。

    服务端无状态且 SSE 无双向通道，审批只能分段：客户端须把 `tool_calls` 原样持久化并在续跑时回传，
    缺失的决定一律按拒绝处理（见 `app/tools/approval.py`）；`description` / `risk_level` 是判断所需的最小上下文。
    """

    type: Literal["approval_required"] = "approval_required"
    #: 待批准的调用。**顺序即模型发起顺序**，用户可按同序逐条决定。
    tool_calls: list[ApprovalToolCall]


class StreamError(BaseModel):
    """本轮失败；`message` 只允许放可外泄文案（内部异常只给关联 ID，见 `stream_chat_events`）。
    `retryable` 只决定客户端是否显示重试按钮：401/403/400/404 标 false，同一个请求再发结果不变。
    `code`：`context_overflow` 重试必败、`model_unavailable` 配置里没有该模型、`internal_error` 是服务端自身错。
    """

    type: Literal["error"] = "error"
    message: str
    retryable: bool = False
    code: Literal["context_overflow", "model_unavailable", "internal_error"] | None = None


#: SSE 事件载荷。判别键是 `type`，客户端可以直接按它 switch。
ChatEvent = Annotated[
    ContentDelta
    | ThinkingDelta
    | ToolCall
    | ToolResult
    | ClarificationRequest
    | ApprovalRequiredEvent
    | FallbackUsed
    | ContextCompacted
    | Usage
    | Done
    | StreamError,
    Field(discriminator="type"),
]

__all__ = [
    "ChatEvent",
    "ClarificationRequest",
    "ContentDelta",
    "ContextCompacted",
    "Done",
    "FallbackUsed",
    "StreamError",
    "ThinkingDelta",
    "ToolCall",
    "ToolResult",
    "Usage",
    "UsageTiming",
]
