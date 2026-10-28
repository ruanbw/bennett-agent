"""沙箱的两个客户端契约：心跳与状态。

## 为什么请求里只有 session_id，没有 lease_id

租约由服务端按 `(登录态 user_id, session_id)` 解析。客户端手里没有可伪造的租约句柄——
和工具层同一个设计决定：越权在构造上不可达，
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


class SandboxArtifact(BaseModel):
    """回收后导出的一份产物。"""

    name: str = Field(description="文件名（归档名或清单名）")
    size_bytes: int = Field(description="对象大小（字节）")
    download_url: str = Field(description="预签名下载直链（有时效，别缓存、别转发）")
    expires_at: datetime = Field(description="链接失效时间")


class SandboxArtifactsResponse(BaseModel):
    """「我的电脑」的产物清单。

    机器销毁前会把 workspace 打包导出（见 `app/services/sandbox/artifacts.py`），
    `artifact_uri` 只是对象存储里的键——用户拿不到它。这个端点是那个键的**唯一出口**：
    把键翻成有时效的预签名直链。

    `state` 与 `lease_id` 一起返回，是因为前端要能区分三种空：从来没有过机器
    （`lease_id=null`）、机器还在（没产物，因为还没销毁）、机器已销毁但没有产物
    （导出失败，见 `lease.error`）。只给一个空数组会把这三件事混成一句「没有」。
    """

    session_id: str = Field(description="请求里带的会话标识（仅回显，不参与查询）")
    lease_id: str | None = Field(default=None, description="最近一条租约的 id；从未创建过为 null")
    state: str | None = Field(default=None, description="租约状态；从未创建过为 null")
    artifacts: list[SandboxArtifact] = Field(
        default_factory=list,
        description="已导出的产物；没有产物（还没销毁/导出失败/未配置对象存储）时为空数组",
    )
