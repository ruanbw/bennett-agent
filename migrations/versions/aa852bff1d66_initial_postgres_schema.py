"""initial postgres schema

这是**基线迁移**：在一个空库上直接执行即可得到完整结构。

它替代了 SQLite 时代的两套东西：

1. `Base.metadata.create_all`（建表）；
2. `app/db.py::_migrate_user_role_column`（手写幂等补列）。

## 为什么这里全是 sa.Uuid()

SQLite 时代 `user.id` 是 `CHAR(36)`（fastapi-users 的 GUID）、而我们自己的列是
`CHAR(32)`（SQLAlchemy 的 Uuid）——两者字面量不同，外键约束会直接拒绝，曾经造成过
`FOREIGN KEY constraint failed` 的 500。Postgres 上两者都渲染成原生 `UUID`，这个
陷阱从根上消失了。当前所有 UUID 列统一用 `sa.Uuid()`。

## 可读性约定

后续迁移请写清「为什么改」而不是只写「改了什么」：半年后排查问题时，`ALTER TABLE`
本身没有信息量，当时的约束与取舍才是。

Revision ID: aa852bff1d66
Revises:
Create Date: 2026-10-23 19:08:06.733180+00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

#: revision 标识符（Alembic 在 downgrade 与 `alembic history` 里用它定位）
revision: str = "aa852bff1d66"
#: 上一个 revision。`None` 表示这是初始迁移——它必须能在**空库**上直接执行。
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 建表顺序由外键依赖决定（Alembic 已排好）：user 必须先于所有引用它的表。
    op.create_table(
        "app_setting",
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
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
    op.create_table(
        "model_provider",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("slug", sa.String(length=60), nullable=False),
        sa.Column("base_url", sa.String(length=500), nullable=False),
        sa.Column("api_key", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_model_provider_slug"), "model_provider", ["slug"], unique=True)
    op.create_table(
        "user",
        sa.Column("role", sa.String(length=20), server_default="user", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("hashed_password", sa.String(length=1024), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_superuser", sa.Boolean(), nullable=False),
        sa.Column("is_verified", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_user_email"), "user", ["email"], unique=True)
    op.create_table(
        "accesstoken",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token", sa.String(length=43), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="cascade"),
        sa.PrimaryKeyConstraint("token"),
    )
    op.create_index(op.f("ix_accesstoken_created_at"), "accesstoken", ["created_at"], unique=False)
    op.create_table(
        "conversation",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("reasoning_effort", sa.String(length=32), nullable=True),
        sa.Column("is_pinned", sa.Boolean(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False),
        sa.Column("client_rev", sa.Integer(), nullable=False),
        sa.Column("seq_counter", sa.Integer(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_conversation_pull", "conversation", ["user_id", "version"], unique=False)
    op.create_table(
        "knowledge_document",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("source", sa.String(length=500), nullable=True),
        sa.Column("embedding_model", sa.String(length=200), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_knowledge_document_user", "knowledge_document", ["user_id", "created_at"], unique=False)
    op.create_table(
        "provider_model",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider_id", sa.Uuid(), nullable=False),
        sa.Column("model_id", sa.String(length=200), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("reasoning", sa.Boolean(), nullable=True),
        sa.Column("supported_reasoning_levels", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("context_window", sa.Integer(), nullable=True),
        sa.Column("max_output_tokens", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["provider_id"], ["model_provider.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_id", "model_id", name="uq_provider_model"),
    )
    op.create_index(op.f("ix_provider_model_provider_id"), "provider_model", ["provider_id"], unique=False)
    op.create_table(
        "sync_counter",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_table(
        "knowledge_chunk",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("heading", sa.String(length=200), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", sa.LargeBinary(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_document.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_knowledge_chunk_document", "knowledge_chunk", ["document_id", "chunk_index"], unique=False)
    op.create_index("ix_knowledge_chunk_user", "knowledge_chunk", ["user_id"], unique=False)
    op.create_table(
        "message",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("client_rev", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversation.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "seq", name="uq_message_conversation_seq"),
    )
    op.create_index("ix_message_conversation", "message", ["conversation_id", "seq"], unique=False)
    op.create_index("ix_message_pull", "message", ["user_id", "version"], unique=False)
    op.create_table(
        "uploaded_file",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_document.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "sha256", name="uq_uploaded_file_user_sha256"),
    )
    op.create_index("ix_uploaded_file_queue", "uploaded_file", ["status", "created_at"], unique=False)
    op.create_index("ix_uploaded_file_user", "uploaded_file", ["user_id", "created_at"], unique=False)


def downgrade() -> None:
    # 反向顺序删表：先删引用方，后删被引用方。
    # 生产环境不会走到这里（回滚靠“再写一个向前的迁移”），但本地与 CI 需要它可用。
    op.drop_index("ix_uploaded_file_user", table_name="uploaded_file")
    op.drop_index("ix_uploaded_file_queue", table_name="uploaded_file")
    op.drop_table("uploaded_file")
    op.drop_index("ix_message_pull", table_name="message")
    op.drop_index("ix_message_conversation", table_name="message")
    op.drop_table("message")
    op.drop_index("ix_knowledge_chunk_user", table_name="knowledge_chunk")
    op.drop_index("ix_knowledge_chunk_document", table_name="knowledge_chunk")
    op.drop_table("knowledge_chunk")
    op.drop_table("sync_counter")
    op.drop_index(op.f("ix_provider_model_provider_id"), table_name="provider_model")
    op.drop_table("provider_model")
    op.drop_index("ix_knowledge_document_user", table_name="knowledge_document")
    op.drop_table("knowledge_document")
    op.drop_index("ix_conversation_pull", table_name="conversation")
    op.drop_table("conversation")
    op.drop_index(op.f("ix_accesstoken_created_at"), table_name="accesstoken")
    op.drop_table("accesstoken")
    op.drop_index(op.f("ix_user_email"), table_name="user")
    op.drop_table("user")
    op.drop_index(op.f("ix_model_provider_slug"), table_name="model_provider")
    op.drop_table("model_provider")
    op.drop_index(op.f("ix_model_price_model_id"), table_name="model_price")
    op.drop_table("model_price")
    op.drop_table("app_setting")
