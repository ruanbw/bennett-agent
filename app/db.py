"""数据库基础设施：异步引擎、ORM 基类，以及用户 / 会话 / 模型提供商三组表。

## 为什么是 SQLite

本项目的写入压力很低：流式对话按 800ms 节流落库，限流上限 30 请求/分/账号，
单进程部署。即使 100 路对话同时进行也只有约 125 次单行写/秒，SQLite（WAL 模式）
的余量在两三个数量级。存储量同理，远未接近其上限。

**唯一会迫使换库的情况是多机部署**——SQLite 文件是本地的，多实例会各写各的。
届时换 Postgres 只需改 `database_url` 与这里的引擎构造。

## 各组表的职责

- `user`：fastapi-users 的标准用户表（邮箱 + 密码哈希 + 三个布尔位）。
  `is_active=False` 就是「封禁」：认证依赖要求 active=True，且封禁时会删掉该用户的
  全部会话行，所以在途请求与刷新页面都会立刻失效。
- `access_token`：**服务端会话**。一条记录 = 一个登录态。这是「不用 JWT」的落点：
  token 只是一串随机值，状态在库里，因此登出/踢人/改密失效都是删一行，立即生效。
- `model_provider` / `provider_model`：管理后台维护的模型提供商与模型清单。
  提供商保存 base_url 与 API key（每个提供商可以指向不同的网关或官方 API），模型行
  保存上游模型名、展示名与**启用开关**。对外路由键是 `{provider.slug}/{model_id}`，
  因为不同提供商可能提供同名模型。
"""

import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends
from fastapi_users.db import SQLAlchemyBaseUserTableUUID, SQLAlchemyUserDatabase
from fastapi_users_db_sqlalchemy.access_token import (
    SQLAlchemyAccessTokenDatabase,
    SQLAlchemyBaseAccessTokenTableUUID,
)
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    event,
)
from sqlalchemy import Uuid as SQLUuid
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class User(SQLAlchemyBaseUserTableUUID, Base):
    """用户表。

    字段由 fastapi-users 定义：`id`(UUID) / `email` / `hashed_password` /
    `is_active` / `is_superuser` / `is_verified`。

    - `is_active=False` 是**封禁**：认证依赖要求 active=True，且封禁接口会同时删除
      该用户的全部会话行，因此在途请求与刷新都会立即生效。
    - `is_superuser=True` 是**管理后台**的入口。没有任何接口能把自己提升为管理员，
      只能由部署者通过 `SUPERUSER_EMAILS` 环境变量指定。
    """


class AccessToken(SQLAlchemyBaseAccessTokenTableUUID, Base):
    """会话表：`token` / `user_id` / `created_at`。

    `user_id` 带 `ondelete="cascade"`，但 SQLite 默认**不启用**外键约束，
    所以下面必须显式 `PRAGMA foreign_keys=ON`，否则删用户后会话记录会残留。
    """


class ModelProvider(Base):
    """模型提供商：一个可独立寻址的 OpenAI 兼容端点。

    `base_url` / `api_key` 是**运行时**凭据，不再只来自环境变量：管理后台可以接入
    多个提供商（本机 LiteLLM 网关、官方 API、自建 vLLM……），每个提供商用自己的
    凭据与模型清单。

    `slug` 是生成一次、之后不随改名变化的稳定标识——它参与对外模型路由键
    (`{slug}/{model_id}`)，改名不能把用户已保存的模型选择悄悄改掉。

    **API key 明文存库**：本项目是单机部署，`.env` 里的密钥同样是明文。对外 API
    永远只返回掩码（见 `app/schemas/admin.py:mask_api_key`），并在文档里注明：
    需要更强隔离时应改用密钥管理服务，而不是在这里加一层自造加密。
    """

    __tablename__ = "model_provider"

    id: Mapped[uuid.UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    slug: Mapped[str] = mapped_column(String(60), nullable=False, unique=True, index=True)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    """极简键值表，目前只存「模型注册表是否已经初始化」。

    为什么需要这个标记：库里一条提供商都没有时有两种完全不同的含义——

    1. 全新部署，管理后台还没配置（此时应该退回旧的 `LITELLM_API_BASE` 路径，
       否则升级后第一次启动就没有任何模型可用）；
    2. 管理员把提供商全删了（此时应该**没有任何模型**，而不是悄悄恢复环境变量配置）。

    只看 `model_provider` 有没有行无法区分两者。初始化标记在首次引导或首次创建
    提供商时写入，之后永不清除——删光提供商仍然维持「数据库是权威」。
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
    """提供商下的一个模型；`enabled` 是对用户是否可见/可选的开关。

    `model_id` 是**发给上游的名字**（例如 `openai/gpt-4o` 或 `deepseek-chat`），
    不参与 URL。对外键由 `provider.slug` + `model_id` 拼成：不同提供商可以各自
    提供同名模型，直接拿 `model_id` 做键会互相覆盖。

    `(provider_id, model_id)` 唯一：同步上游列表时按这个组合 upsert，重复同步不会
    产生重复行，也不会把管理员手动调过的 `enabled` / 展示名覆盖回去。
    """

    __tablename__ = "provider_model"
    __table_args__ = (UniqueConstraint("provider_id", "model_id", name="uq_provider_model"),)

    id: Mapped[uuid.UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider_id: Mapped[uuid.UUID] = mapped_column(
        SQLUuid(as_uuid=True),
        ForeignKey("model_provider.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    reasoning: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    supported_reasoning_levels: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
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


def _resolve_database_url(url: str) -> str:
    """把相对的 SQLite 路径固定到项目根目录。

    `sqlite+aiosqlite:///./bennett.db` 是相对**当前工作目录**解析的。从别的目录
    启动（例如 `python /path/to/main.py` 或在 IDE 里跑测试）就会悄悄创建出第二个
    空库，表现为「注册过的账号不见了」。这里统一锚到 `BASE_DIR`。
    """
    prefix = "sqlite+aiosqlite:///"
    if not url.startswith(prefix):
        return url

    raw_path = url[len(prefix) :]
    # 已是绝对路径（/xxx），或内存库（:memory:），都原样返回
    if raw_path.startswith("/") or raw_path.startswith(":memory:"):
        return url

    absolute = (Path(__file__).resolve().parent.parent / raw_path).resolve()
    return f"{prefix}{absolute}"


DATABASE_URL = _resolve_database_url(settings.database_url)

engine = create_async_engine(DATABASE_URL)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


if engine.dialect.name == "sqlite":

    @event.listens_for(engine.sync_engine, "connect")
    def _apply_sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
        """SQLite 的 PRAGMA 是**每连接**生效，不是数据库级的持久设置。

        - `foreign_keys=ON`：SQLite 默认关闭外键约束，不开则级联删除失效。
        - `journal_mode=WAL`：读写不互相阻塞，避免流式对话期间的写锁卡住读请求。
        - `busy_timeout=5000`：并发写入时排队等待而不是立刻抛 `database is locked`。
        """
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


async def create_db_and_tables() -> None:
    """启动时建表。

    新增的 `model_provider` / `provider_model` 两张表由 `create_all` 自动创建；
    已有表（user / access_token）不会被动到。

    **没有引入 Alembic**：当前还没有「改已有列」的迁移需求，多一套工具链不划算。
    注意 `create_all` 只创建缺失的表，**不会修改已存在的表结构**；一旦需要给现有
    表加列，应改用 Alembic，而不是指望这里生效。
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("数据库就绪: %s", DATABASE_URL)


# ---------------------------------------------------------------------
# 会话维护
# ---------------------------------------------------------------------
async def cleanup_expired_access_tokens(lifetime_seconds: int) -> int:
    """删除已过期的会话行，返回删除条数。

    为什么需要显式清理：`DatabaseStrategy.read_token` 只把 `created_at` 当**过滤条件**
    （`get_by_token(token, max_age)` 是 `WHERE created_at >= max_age` 的 SELECT），
    **从不删除过期行**。没有这个维护动作，`access_token` 表会随登录次数无限增长，
    已过期的会话 token 也长期留在库里。

    用独立会话（`async_session_maker`）而不是请求级 session：调用方（登录钩子、
    将来的启动清理）通常正处于自己的事务里，清理是与该请求无关的维护动作。
    """
    if lifetime_seconds <= 0:
        # 与 services/auth.py 里 get_database_strategy 的防御同源：lifetime<=0 时
        # cutoff 会落在现在甚至未来，把**全部有效会话**当成过期删掉，比不清理危险得多。
        raise ValueError(
            f"cleanup_expired_access_tokens 的 lifetime_seconds 必须 > 0，当前为 {lifetime_seconds}："
            "0/负值会让服务端会话永不过期，按此清理会误删全部有效会话。"
        )

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=lifetime_seconds)
    async with async_session_maker() as session:
        result = await session.execute(delete(AccessToken).where(AccessToken.created_at < cutoff))
        await session.commit()
        return result.rowcount


async def delete_user_access_tokens(user_id: uuid.UUID) -> int:
    """删除某个用户的**全部**会话行，返回删除条数（改密 / 踢人 / 停用账号用）。

    只按 `user_id` 过滤：同一部署下改 A 的密码不能把 B 踢下线。
    """
    async with async_session_maker() as session:
        result = await session.execute(delete(AccessToken).where(AccessToken.user_id == user_id))
        await session.commit()
        return result.rowcount


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session


async def get_user_db(session: AsyncSession = Depends(get_async_session)):  # noqa: B008
    yield SQLAlchemyUserDatabase(session, User)


async def get_access_token_db(session: AsyncSession = Depends(get_async_session)):  # noqa: B008
    yield SQLAlchemyAccessTokenDatabase(session, AccessToken)
