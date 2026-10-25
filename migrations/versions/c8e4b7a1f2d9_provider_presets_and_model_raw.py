"""提供商预设字段与模型原始元数据

两件事：

- `model_provider` 增加 `auth_style` / `extra_headers` / `preset_key`：内置厂商预设
  （OpenAI、Anthropic……）创建提供商时，认证头风格与固定请求头来自预设并**落库**，
  之后运行时只看库、不看预设目录（目录会更新，已建的提供商不能悄悄跟着变）。
- `provider_model` 增加 `raw`：同步时把上游条目原样存下来，后台可查「上游到底报了
  什么」——解析白名单只挑已知字段，其余信息此前在解析那一刻就被丢掉了。

`auth_style` / `extra_headers` 必须带 server_default：ALTER TABLE 不会执行 ORM 的
Python 侧默认值，存量行没有默认值会让 NOT NULL 约束直接失败。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c8e4b7a1f2d9"
down_revision: str | None = "e1a9c07d4f2b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_provider",
        sa.Column("auth_style", sa.String(length=16), server_default="bearer", nullable=False),
    )
    op.add_column(
        "model_provider",
        sa.Column(
            "extra_headers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("model_provider", sa.Column("preset_key", sa.String(length=40), nullable=True))
    op.add_column("provider_model", sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("provider_model", "raw")
    op.drop_column("model_provider", "preset_key")
    op.drop_column("model_provider", "extra_headers")
    op.drop_column("model_provider", "auth_style")
