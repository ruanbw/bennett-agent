"""沙箱租约与命令审计：租约为长生命周期资源，命令表只作审计与排障。"""

import uuid
from datetime import datetime, timezone

from fastapi_users_db_sqlalchemy.generics import GUID
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SandboxLease(Base):
    """一台沙箱机器的租约（用户 × 会话 × 一台机器）。

    机器是服务端独有的长生命周期资源，多 worker 下只能落库；`user_id` 的部分唯一索引（仅活动状态）
    强制每用户至多一台，兼作同轮并发建机去重；状态迁移必须用条件 UPDATE 抢占，不能先读后写。
    """

    __tablename__ = "sandbox_lease"
    __table_args__ = (
        Index(
            "ux_sandbox_lease_one_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("state IN ('provisioning', 'ready', 'paused')"),
        ),
        # 回收循环的索引：WHERE state='ready' AND hard_expires_at < now()
        Index("ix_sandbox_lease_reap", "state", "hard_expires_at"),
    )

    #: `sbx_<32 hex>`。服务端生成、随机，不作为凭据使用（凭据是登录态 + 作用域）。
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: 最后使用这台机器的会话标识 `sess_<uuid>`：归因用，不参与寻址，更不是身份
    #: （身份始终来自服务端注入的作用域，见 `app/tools/sandbox_scope.py`）。
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: `e2b` / `docker`。换后端时旧租约要靠它找到该让谁回收。
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    #: provider 侧句柄（E2B sandbox id / 容器名）。**只在服务端流动**，绝不进工具参数。
    provider_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    template: Mapped[str] = mapped_column(String(128), nullable=False)
    template_version: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    #: 最后一次「服务端观察到的工作」（exec / 文件读写）。与 Redis 里带 TTL 的心跳分开：
    #: 心跳证明客户端还在，这个证明机器还在被用，于是关掉页面不影响在跑的长任务。
    last_activity: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: 绝对上限。只靠空闲回收的话，一个挂着不动的标签页可以永远付费占机。
    hard_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    destroyed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: 进入 `destroying` 的时刻（回收循环据此区分「正在销毁」与「销毁被中断」）。
    destroying_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: idle / hard_ttl / session_deleted / user_banned / user_reset / error
    destroy_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: 销毁前导出的产物（对象存储键）。产物比机器重要：机器可以重建，用户的计算结果不行。
    artifact_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: 最近一次失败原因（给运维看，不进模型上下文）。
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class SandboxCommand(Base):
    """沙箱内的每一条命令（审计 + 排障）；不参与任何金额计算，计量只有 token。

    这张表的存在理由是边界审批模型：既然不再逐条命令打断用户，就必须事后能查清跑过什么。
    `output_head` 存渲染给模型看的同一份截断文本而非原始输出（可能是用户数据），后者留在沙箱里。
    """

    __tablename__ = "sandbox_command"
    __table_args__ = (Index("ix_sandbox_command_lease", "lease_id", "seq"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    lease_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sandbox_lease.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: 会话内的自增序号，让审计可以按顺序回放。
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    command: Mapped[str] = mapped_column(Text, nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_head: Mapped[str | None] = mapped_column(Text, nullable=True)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
