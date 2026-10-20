"""跨设备同步的请求 / 响应形状。

协议的三条硬约束，改动前先读：

1. **游标是不透明的**。客户端只负责保存与回传，不解析、不比较、不自己构造。
   服务端可以随时换内部编码（`v1:1234` → `v2:...`），只要不认识就回 410 让客户端
   重来一次全量同步。这是 Microsoft Graph delta query / Matrix `next_batch` 的同一套
   做法：客户端一旦开始解释游标，服务端就再也换不动编码。
2. **id 由客户端生成**（`sess_…` / `msg_…`）。同步接口必须幂等，而服务端发主键会把
   「重试」变成「多一行」。
3. **逐行拒绝**。一批 200 行里有一条超长消息，不该让另外 199 条一起失败——客户端
   拿到 `rejected` 后只需把这几行标成错误，其余照常算成功。

会话与消息都带 `client_rev`：客户端自己的修订号（每次本地修改 +1），服务端用它拒绝
「旧内容覆盖新内容」的重放。它不是时间戳，不参与任何跨设备比较。
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: 游标前缀。换编码格式时改这里，旧游标会被识别为「不认识」并触发 410 重同步。
CURSOR_PREFIX = "v1"

#: id 的字符集与长度。刻意**不**强制 `sess_` / `msg_` 前缀：老版本客户端可能留下
#: 别的形状，前缀只用于可读性，不承担校验职责。真正的约束是「别把我当 SQL」。
ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"


class ConversationIn(BaseModel):
    """一次 push 里的一个会话行。字段与前端 `SessionRecord` 对齐。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ID_PATTERN)
    title: str = Field(default="", max_length=2000)
    model: str | None = Field(default=None, max_length=255)
    reasoning_effort: str | None = Field(default=None, max_length=32)
    is_pinned: bool = False
    #: 墓碑：删除只推这一位，服务端据此级联删消息并让其他设备也删掉。
    deleted: bool = False
    #: 客户端修订号（本地每次修改 +1）；服务端拒绝更旧的重放。
    client_rev: int = Field(default=0, ge=0)
    #: 客户端毫秒时间戳，只用于展示排序，**不可**作为同步判据。
    created_at: int = Field(ge=0)
    updated_at: int = Field(ge=0)


class MessageIn(BaseModel):
    """一次 push 里的一条消息行。

    `payload` 整包透传（正文、思考链、工具调用与结果、用量、错误分类……）。
    服务端不理解也不需要理解它的内部结构，只校验大小与角色。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ID_PATTERN)
    conversation_id: str = Field(pattern=ID_PATTERN)
    role: Literal["user", "assistant"]
    payload: dict[str, Any] = Field(default_factory=dict)
    client_rev: int = Field(default=0, ge=0)
    created_at: int = Field(ge=0)


class PushRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversations: list[ConversationIn] = Field(default_factory=list)
    messages: list[MessageIn] = Field(default_factory=list)


class PushAccepted(BaseModel):
    conversations: int = 0
    messages: int = 0


#: 逐行拒绝的原因。客户端按它决定「重试 / 丢弃 / 提示用户」。
RejectReason = Literal[
    #: 这一行的 id 属于另一个用户。id 是 uuid v4（不可猜测），因此这里允许区分
    #: 「存在但不是我的」与「不存在」——省掉一次无差别返回，也便于客户端排查。
    "not_owned",
    #: 服务端已有更新的 `client_rev`：这是一次过期的重放，丢掉即可。
    "stale",
    #: payload 序列化后超过 `SYNC_PAYLOAD_MAX_BYTES`。
    "too_large",
    #: 会话不存在（既不在本批，也不是自己名下的行）。
    "conversation_missing",
    #: 会话已被删除（墓碑已存在），不能再往里写消息。
    "conversation_deleted",
    #: 触到单用户会话数 / 单会话消息数上限。
    "quota_exceeded",
]


class RejectedRow(BaseModel):
    """被拒绝的一行。`reason` 是机器可读的枚举，客户端据此决定「重试 / 丢弃 / 提示」。"""

    kind: Literal["conversation", "message"]
    id: str
    reason: RejectReason


class PushResult(BaseModel):
    #: 本次 push 之后该用户的同步版本（写入了行则前进，纯重试则不变）。
    version: int
    accepted: PushAccepted
    rejected: list[RejectedRow] = Field(default_factory=list)


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    model: str | None
    reasoning_effort: str | None
    is_pinned: bool
    deleted: bool
    client_rev: int
    version: int
    created_at: int
    updated_at: int


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    conversation_id: str
    role: str
    payload: dict[str, Any]
    client_rev: int
    #: 服务端赋值的会话内顺序。客户端有它就按它排，没有（本地未同步）才退回时间戳。
    seq: int
    version: int
    created_at: int


class PullResult(BaseModel):
    """增量拉取结果。

    `cursor` 是**下一次请求要原样回传**的值。当 `has_more` 为真时它停在「两路都
    拉完」的位置，剩余行在下一次 pull 继续；同一行可能被重复返回，客户端必须能
    幂等应用（Graph 的文档也是这么要求的：replay 是正常现象）。
    """

    cursor: str
    has_more: bool = False
    conversations: list[ConversationOut] = Field(default_factory=list)
    messages: list[MessageOut] = Field(default_factory=list)
