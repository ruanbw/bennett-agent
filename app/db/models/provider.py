"""模型提供商与模型清单：`slug` 参与对外路由键，`api_key` 落库加密。"""

import uuid
from datetime import datetime, timezone

from fastapi_users_db_sqlalchemy.generics import GUID
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, EncryptedText


class ModelProvider(Base):
    """模型提供商：一个可独立寻址的 OpenAI 兼容端点，各自带运行时凭据与模型清单。

    `slug` 参与对外路由键 `{slug}/{model_id}`，故生成后不随改名变化，否则用户已保存的
    模型选择会被悄悄改掉。`api_key` 落库加密，对外只返回掩码（`admin.py:mask_api_key`）。
    """

    __tablename__ = "model_provider"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    slug: Mapped[str] = mapped_column(String(60), nullable=False, unique=True, index=True)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    api_key: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)
    #: 认证方式：Key 放进哪个请求头（bearer / x-api-key / x-goog-api-key / none），由预设写入。
    #: server_default 必须与 sql/init.sql 一致，否则建库脚本与 metadata 会分叉。
    auth_style: Mapped[str] = mapped_column(String(16), nullable=False, default="bearer", server_default="bearer")
    #: 随请求固定携带的头（如 Anthropic 的 `anthropic-version`）：不能只存在预设里，
    #: 预设更新时库里的行不该悄悄跟着变。
    extra_headers: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    #: 创建时使用的预设 key；自定义添加为 None。仅供后台展示与排查。
    preset_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    models: Mapped[list["ProviderModel"]] = relationship(
        back_populates="provider",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class AppSetting(Base):
    """极简键值表，目前只存「模型注册表是否已经初始化」这个标记。

    只看 `model_provider` 有没有行，分不清「全新部署」与「管理员删光了提供商」：前者该退回
    `LITELLM_API_BASE`，后者该没有模型。标记一经写入永不清除，删光后仍维持数据库权威。
    """

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class ProviderModel(Base):
    """提供商下的一个模型；`enabled` 是对用户是否可见、可选的开关。

    `model_id` 是发给上游的名字，不参与 URL；对外键必须拼上 `provider.slug`，否则不同
    提供商的同名模型会互相覆盖。`(provider_id, model_id)` 唯一，重复同步 upsert 不会重复建行。
    """

    __tablename__ = "provider_model"
    __table_args__ = (UniqueConstraint("provider_id", "model_id", name="uq_provider_model"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    provider_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("model_provider.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    reasoning: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: JSONB 而非 JSON：可建表达式索引，且不必每次读取都重新解析文本。
    supported_reasoning_levels: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 上游最近一次同步返回的原始条目（`/v1/models` 的 data 元素原样保存）：白名单之外的
    #: 字段都在这里，后台据此回答「上游到底报了什么」。手动添加的为 NULL，只在同步时覆盖。
    raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    provider: Mapped[ModelProvider] = relationship(back_populates="models")
