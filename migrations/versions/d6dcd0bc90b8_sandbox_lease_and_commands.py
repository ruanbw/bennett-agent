"""sandbox lease and command log

## 为什么这两张表是「服务端唯一真相」

会话历史仍然由客户端回放，但**沙箱机器是服务端独有的长生命周期资源**：它上面有什么
文件、跑了什么进程，客户端既不知道也无法重建。于是「这台机器存在、属于谁、何时必须
销毁」只能落在库里——多 worker 部署下内存态必然对不上，进程重启还会丢掉回收所需的
全部信息（既回收不到机器，也收不到账）。

`sandbox_command` 是同一份真相的审计面：改成「边界审批」之后不再逐条命令打断用户，
代价是必须事后能查清跑过什么。它**不参与任何金额计算**（本项目的计量只有 token），
所以只存审计需要的最小集合；`output_head` 存的是渲染给模型看的同一份截断文本，
原始输出既不必要（审计问的是「模型当时看到了什么」）也不该落库（可能是用户数据）。

## 唯一索引为什么同时是并发创建的去重机制

`ux_sandbox_lease_one_per_session` 不只是「一个会话一台机器」的约束，它还是**并发创建
的去重点**：agent 在一轮里并发发起两个沙箱工具调用是真实存在的，两个请求都会读到
「没有活动租约」，只在应用层先查后写无法裁决（两个事务看到的是同一个空集）。必须由
数据库的唯一索引裁决，让后到的 `INSERT` 撞约束、转而重读已存在的租约。

索引因此只覆盖活动状态（`provisioning/ready/paused`）：历史行可以无限堆积，同一会话
重建环境也不会撞唯一约束。

`ix_sandbox_lease_reap` 服务于回收循环的查询 `WHERE state = 'ready' AND
hard_expires_at < now()`——只靠空闲回收的话，一个挂着不动的标签页可以永远付费占机，
所以绝对上限必须能被廉价地扫出来。

## 刻意不写 server_default

模型侧用的是 Python 默认值（`datetime.now(timezone.utc)`、`truncated=False`）。
`env.py` 打开了 `compare_server_default`，这里若补一个 `server_default=now()`，
`alembic check` 会立刻报漂移；而「两边都有默认值、取值时机却不同」正是最难排查的
一类分歧（谁先写、时钟是谁的、批量插入时是否每行都不同）。

Revision ID: d6dcd0bc90b8
Revises: aa852bff1d66
Create Date: 2026-10-23 22:48:33.057820+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

#: revision 标识符（Alembic 在 downgrade 与 `alembic history` 里用它定位）
revision: str = "d6dcd0bc90b8"
#: 上一个 revision：初始 Postgres schema。
down_revision: str | None = "aa852bff1d66"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 先建被引用方：sandbox_command.lease_id 有指向 sandbox_lease 的外键。
    op.create_table(
        "sandbox_lease",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_ref", sa.String(length=255), nullable=True),
        sa.Column("template", sa.String(length=128), nullable=False),
        sa.Column("template_version", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_activity", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hard_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("destroyed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("destroy_reason", sa.String(length=32), nullable=True),
        sa.Column("artifact_uri", sa.String(length=512), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # 部分唯一索引：既是「一会话一机」的强制点，也是并发创建的去重点（见模块 docstring）。
    op.create_index(
        "ux_sandbox_lease_one_per_session",
        "sandbox_lease",
        ["user_id", "session_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('provisioning', 'ready', 'paused')"),
    )
    op.create_index("ix_sandbox_lease_reap", "sandbox_lease", ["state", "hard_expires_at"], unique=False)
    op.create_table(
        "sandbox_command",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("lease_id", sa.String(length=64), nullable=False),
        # 会话内自增序号，让审计可以按顺序回放。
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("output_bytes", sa.BigInteger(), nullable=True),
        sa.Column("output_head", sa.Text(), nullable=True),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # 租约删掉后命令日志没有独立意义（机器都没了，审计也失去上下文）。
        sa.ForeignKeyConstraint(["lease_id"], ["sandbox_lease.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sandbox_command_lease", "sandbox_command", ["lease_id", "seq"], unique=False)


def downgrade() -> None:
    # 反向顺序删表：先删引用方，后删被引用方。
    # 生产不会走到这里（回滚靠再写一个向前的迁移），但本地与 CI 需要它真的可用。
    op.drop_index("ix_sandbox_command_lease", table_name="sandbox_command")
    op.drop_table("sandbox_command")
    op.drop_index("ix_sandbox_lease_reap", table_name="sandbox_lease")
    op.drop_index("ux_sandbox_lease_one_per_session", table_name="sandbox_lease")
    op.drop_table("sandbox_lease")
