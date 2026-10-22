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
    """工具调用返回。

    `outcome` 是 pydantic-ai 给的结构化执行结果：工具抛错被隔离、
    权限拒绝、中断，都与「成功返回一段文字」语义不同。前端据此显示失败状态，
    而不是把隔离后的错误文案当成一次成功的工具调用。
    """

    type: Literal["tool_result"] = "tool_result"
    tool_call_id: str
    tool_name: str
    result: str
    outcome: Literal["success", "failed", "denied", "interrupted"] = "success"


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


class FallbackUsed(BaseModel):
    """主模型失败，本轮已自动切换到备用模型。

    为什么要有这个事件：切换本身是静默的，但用户看到的回答来自另一个模型。
    不告诉用户，排障时既不知道这轮是谁答的，也会把备用模型的能力差异误判成
    「这个模型变差了」。事件只带模型 id 与一句**类别**短语，不带上游错误原文
    （原文可能含上游 URL 与凭据上下文，只进日志）。
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


class Usage(BaseModel):
    """本轮的**真实** token 用量（来自上游响应，不是估算）。

    `type` 只能说明「这是一个用量事件」，所以这里把口径写在字段名里：

    - `context_tokens` 是**本轮最后一次**模型请求的 prompt token 数，也就是当前
      上下文的长度。一轮里如有工具调用会有多次请求，累加值会把上下文算大
      （每次请求都重发一遍历史），所以此处不提供累加值。
    - `context_window` 是该模型的原始窗口；`context_limit_tokens` 才是后端判定
      超限时使用的上限（窗口扣除为回答预留的空间、再乘安全系数）。两者不同：
      32k 窗口的模型可能只剩 19k 能放历史，前端拿原始窗口算占比会严重低估占用。
      没有上下文预算（守卫关闭 / 窗口未知 / 备用模型接手）时为 null。
    - `output_tokens` 是本轮生成的总输出（含工具调用之间的中间输出）。
    - `cache_read_tokens` 是上下文里命中上游缓存的部分，是 `context_tokens` 的
      子集，不是额外开销。

    上游没在流式响应里回用量时**不发这个事件**，不发一个 0。
    """

    type: Literal["usage"] = "usage"
    context_tokens: int
    context_window: int | None = None
    context_limit_tokens: int | None = None
    output_tokens: int = 0
    cache_read_tokens: int = 0


class ContextCompacted(BaseModel):
    """本轮开始前做过上下文压缩（把早期消息折叠成摘要，或截断了超长消息）。

    为什么必须告诉用户：压缩本身是**改了模型看到的上下文**。不说的话，用户会
    把「模型忘了刚才说的」当成模型变笨；运维拿到截图也无从判断是压缩造成的还是
    真的丢消息了。这与「切换备用模型要说明原因」是同一条原则。

    只给数字与策略，不给摘要正文：摘要正文走 pydantic-ai 的运行期 instructions
    （服务端自己拼），不进消息历史，也没有必要回传给客户端（它可能含早期对话的
    敏感内容，而下发会被前端落库并同步到其他设备）。
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


class StreamError(BaseModel):
    """本轮失败。

    `message` 只允许放可以外泄的文案：校验类错误可以给原文，内部异常只能给关联 ID。
    这条边界由 `app/services/chat_service.py::stream_chat_events` 的三条兜底分支
    实现（`_new_error_id` 只把 ID 回给客户端，上游 URL 与 provider 报错上下文只进日志）。

    `retryable` 只用于**客户端是否显示「重试」按钮**：它不代表服务端会自动重试，
    也不保证重试一定成功。凭据/权限问题（401/403）与请求本身的问题（400/404）
    标 false——同一个请求再发一次结果不会变。

    `code` 是给机器看的失败分类，`type` 只能区分「哪一类事件」而区分不了「哪一种
    失败」。取值如下：

    - `context_overflow`：上下文超出模型窗口，重试必然同样失败，用户只能新建会话/
      换更大的模型；
    - `model_unavailable`：数据库配置里这个模型已停用或不存在（典型是管理员在
      请求途中改配置），刷新模型列表就能看到真实清单，重试同一个模型同样失败；
    - `internal_error`：服务端自己出错了（查库失败、模板渲染失败等），**没有**给出
      任何关于模型的确定结论——所以不能归成上面两类，否则会把一次数据库抖动报成
      「模型已下架」，让用户去刷新一个本来正常的列表。已附关联 ID，可重试。

    客户端不该去猜 `message` 里的中文，也不该把「重试按钮」当作万能出路。
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
]
