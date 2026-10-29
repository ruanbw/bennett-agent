"""沙箱的两个客户端契约：心跳与状态。

请求里只有 session_id 没有 lease_id：租约由服务端按 (登录态 user_id, session_id) 解析，
客户端拿不到可伪造的句柄，谎报别人的 session_id 也查不到机器，因为查询一律带 user_id。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

#: 与 `app/schemas/chat.py` 的定义必须逐字一致（`tests/test_sandbox_api.py` 会比对）：
#: 两边不一致的后果是「聊天能用但环境永远被回收」。分两份常量是为了各属其请求契约。
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
    """当前会话的沙箱状态，给前端显示「正在开机 / 已回收」用。

    没有租约时端点返回 404 而不是空对象：`null` 与「查不到」在前端会被混成同一个意思。
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
    """「我的电脑」的产物清单：把 `artifact_uri` 翻成有时效的预签名直链（用户拿不到键本身）。

    产物在机器销毁前打包导出（见 `app/services/sandbox/artifacts.py`）。`state` 与 `lease_id`
    一起返回是为了区分三种空：从未有过机器、机器还在没产物、已销毁但导出失败。
    """

    session_id: str = Field(description="请求里带的会话标识（仅回显，不参与查询）")
    lease_id: str | None = Field(default=None, description="最近一条租约的 id；从未创建过为 null")
    state: str | None = Field(default=None, description="租约状态；从未创建过为 null")
    artifacts: list[SandboxArtifact] = Field(
        default_factory=list,
        description="已导出的产物；没有产物（还没销毁/导出失败/未配置对象存储）时为空数组",
    )
