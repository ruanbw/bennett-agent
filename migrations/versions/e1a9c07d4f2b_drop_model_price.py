"""移除模型价格表

模型价格功能整体下线（管理端维护单价、把每轮 token 用量换算成估算成本的那条链路）。
要看消耗金额，直接查模型提供商自己的后台账单——本项目不再本地维护单价表。

`downgrade` 按初始迁移的原样重建表结构（列、约束、索引一一对应），只恢复**结构**，
不恢复数据：价格是配置不是历史记录，重建后为空是合理状态。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "e1a9c07d4f2b"
down_revision: str | None = "d7df7000bda7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(op.f("ix_model_price_model_id"), table_name="model_price")
    op.drop_table("model_price")


def downgrade() -> None:
    op.create_table(
        "model_price",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("model_id", sa.String(length=300), nullable=False),
        sa.Column("input_per_mtok", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("output_per_mtok", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("cached_input_per_mtok", sa.Numeric(precision=18, scale=6), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_id", name="uq_model_price_model"),
    )
    op.create_index(op.f("ix_model_price_model_id"), "model_price", ["model_id"], unique=False)
