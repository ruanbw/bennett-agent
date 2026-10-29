"""跨设备同步的请求 / 响应形状。
游标对客户端不透明（不认识就回 410）、id 由客户端生成（重试才幂等）、逐行拒绝而非整批失败。
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: 游标前缀。换编码格式时改这里，旧游标会被识别为「不认识」并触发 410 重同步。
CURSOR_PREFIX = "v1"

#: id 的字符集与长度。不强制 `sess_` / `msg_` 前缀（老客户端可能留下别的形状），前缀只管可读性。
ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"

#: 客户端修订号上限：没有它时 `10**20` 会让 PG 的 INTEGER 绑定溢出，变成 500。
MAX_CLIENT_REV = 2**31 - 1

#: 客户端毫秒时间戳上限（9999-12-31T23:59:59.999Z）：超 BIGINT 的输入要在入口变 422 而不是 500。
MAX_CLIENT_TIMESTAMP_MS = 253_402_300_799_999


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
    client_rev: int = Field(default=0, ge=0, le=MAX_CLIENT_REV)
    #: 客户端毫秒时间戳，只用于展示排序，不能作为同步判据。
    created_at: int = Field(ge=0, le=MAX_CLIENT_TIMESTAMP_MS)
    updated_at: int = Field(ge=0, le=MAX_CLIENT_TIMESTAMP_MS)


class MessageIn(BaseModel):
    """一次 push 里的一条消息行。
    `payload` 整包透传（正文、思考链、工具调用与结果……），服务端只校验大小与角色。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=ID_PATTERN)
    conversation_id: str = Field(pattern=ID_PATTERN)
    role: Literal["user", "assistant"]
    payload: dict[str, Any] = Field(default_factory=dict)
    client_rev: int = Field(default=0, ge=0, le=MAX_CLIENT_REV)
    created_at: int = Field(ge=0, le=MAX_CLIENT_TIMESTAMP_MS)


class PushRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversations: list[ConversationIn] = Field(default_factory=list)
    messages: list[MessageIn] = Field(default_factory=list)


class PushAccepted(BaseModel):
    conversations: int = 0
    messages: int = 0


#: 逐行拒绝的原因。客户端按它决定「重试 / 丢弃 / 提示用户」。
RejectReason = Literal[
    #: 这一行的 id 属于另一个用户。id 是 uuid v4（不可猜测），所以区分「不是我的」与「不存在」是安全的。
    "not_owned",
    #: 服务端已有更新的 `client_rev`：过期重放，丢掉即可。
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
    `cursor` 必须原样回传；`has_more` 时它停在「两路都拉完」的位置，同一行可能重复返回，客户端必须幂等。
    """

    cursor: str
    has_more: bool = False
    conversations: list[ConversationOut] = Field(default_factory=list)
    messages: list[MessageOut] = Field(default_factory=list)
