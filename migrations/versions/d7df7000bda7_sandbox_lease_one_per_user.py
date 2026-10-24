"""沙箱租约：一会话一台 → 每用户一台

`user_id` 上的部分唯一索引取代 `(user_id, session_id)`：同一个用户在不同会话里共用
同一台机器。`session_id` 列保留，但降级为「最后是哪个会话在用」的归因信息。

## 为什么先把「多余的活动租约」收掉再建索引

生产库里可能已经存在「同一用户多个活动租约」（旧行为，一会话一台）。索引一旦创建，
这些行会让迁移**直接失败**，而失败发生在部署窗口里——那是最难处理的位置。

处理方式不是删行（删了机器就没人回收，成了孤儿），而是把每个用户除最新一台以外的
活动租约标成 `destroy_failed`：回收循环的候选集**包含**这个状态，于是它们会被正常
拆掉并留下审计痕迹。这比「迁移里直接删记录」安全——机器到底存不存在，只有 provider
知道，而回收循环才是那个该去问的地方。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "d7df7000bda7"
down_revision: str | None = "d6dcd0bc90b8"
branch_labels = None
depends_on = None

#: 活动状态。与 `app/db.py:SandboxLease` 的部分索引谓词、`lease.py:LIVE_STATES` 必须一致。
_ACTIVE = "('provisioning', 'ready', 'paused')"


def upgrade() -> None:
    # 1) 同一用户超出第一台的活动租约 → 交给回收循环处理（见模块 docstring）
    op.execute(
        sa.text(
            f"""
            UPDATE sandbox_lease
               SET state = 'destroy_failed',
                   destroy_reason = 'error',
                   error = '迁移为「每用户一台」前的多余会话机，待回收'
             WHERE state IN {_ACTIVE}
               AND id NOT IN (
                   SELECT DISTINCT ON (user_id) id
                     FROM sandbox_lease
                    WHERE state IN {_ACTIVE}
                    ORDER BY user_id, created_at DESC
               )
            """
        )
    )

    # 2) 换索引：先删旧的再建新的（同名会冲突，不同名会同时生效）
    op.drop_index(
        "ux_sandbox_lease_one_per_session",
        table_name="sandbox_lease",
        postgresql_where=sa.text(f"state IN {_ACTIVE}"),
    )
    op.create_index(
        "ux_sandbox_lease_one_per_user",
        "sandbox_lease",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text(f"state IN {_ACTIVE}"),
    )


def downgrade() -> None:
    """回退到「一会话一台」。

    被标成 `destroy_failed` 的那些行**不恢复**：它们的机器可能已经被回收了，恢复成
    `ready` 会留下「库里说有一台、实际不存在」的租约——那正是需要人工核对的状态。
    """
    op.drop_index(
        "ux_sandbox_lease_one_per_user",
        table_name="sandbox_lease",
        postgresql_where=sa.text(f"state IN {_ACTIVE}"),
    )
    op.create_index(
        "ux_sandbox_lease_one_per_session",
        "sandbox_lease",
        ["user_id", "session_id"],
        unique=True,
        postgresql_where=sa.text(f"state IN {_ACTIVE}"),
    )
