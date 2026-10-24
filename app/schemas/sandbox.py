"""沙箱的两个客户端契约：心跳与状态。

## 为什么请求里只有 session_id，没有 lease_id

租约由服务端按 `(登录态 user_id, session_id)` 解析。客户端手里没有可伪造的租约句柄——
和工具层同一个设计决定（见 `docs/design/sandbox-vm.md` §6/§7）：越权在构造上不可达，
而不是靠一层层校验拦住。谎报别人的 `session_id` 也拿不到别人的机器，因为查询一律带
`user_id`。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

#: 与 `app/schemas/chat.py` 的定义**必须逐字一致**（`tests/test_sandbox_api.py` 会比对）：
#: 同一个 `session_id` 在聊天接口合法、在心跳接口非法的后果是「聊天能用但环境永远被回收」。
#: 两份常量而不是互相 import，是为了让它们各自属于自己的请求契约；一致性由测试锁定。
MAX_SESSION_ID_CHARS = 64
SESSION_ID_PATTERN = rf"^sess_[A-Za-z0-9_-]{{1,{MAX_SESSION_ID_CHARS - 5}}}$"


class SandboxHeartbeatRequest(BaseModel):
    """客户端心跳：只声明「我还在这个会话里」。"""

    session_id: str = Field(
        ...,
        max_length=MAX_SESSION_ID_CHARS,
        pattern=SESSION_ID_PATTERN,
        description="当前打开的会话标识（sess_<...>）",
    )


class SandboxStatusResponse(BaseModel):
    """当前会话的沙箱状态。给前端显示「正在开机 / 已回收」用。

    没有租约时端点返回 404 而不是空对象：`null` 与「查不到」在前端会被混成一个意思，
    而这两者的界面文案不同（「还没开始」vs「不见了」）。
    """

    state: str = Field(description="provisioning / ready / paused / destroying / destroyed / failed")
    expires_at: datetime | None = Field(default=None, description="绝对上限（硬 TTL）到点时间")
    artifact_uri: str | None = Field(
        default=None,
        description="回收后产物在对象存储里的键；没有产物或还没导出时为 null",
    )
