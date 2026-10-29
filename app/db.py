"""数据库基础设施：异步引擎、ORM 基类，以及业务表定义。

表结构的唯一事实来源是 sql/init.sql：整段幂等，启动时执行；其 CREATE TABLE IF NOT EXISTS
对既有表不加列，故启动时逐列比对、漂移即拒绝启动。手写 SQL 里 `user` 必须加双引号。
"""

import base64
import hashlib
import json
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
from fastapi_users_db_sqlalchemy.generics import GUID
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.config import settings
from app.config.secret_box import SecretDecryptError, decrypt_secret, encrypt_secret
from app.roles import ROLE_USER, Role

logger = logging.getLogger(__name__)

#: 项目根目录。sql/init.sql 锚在这里，不能用 CWD：从别的目录启动会找不到建库脚本。
PROJECT_ROOT = Path(__file__).resolve().parent.parent


#: 告警只打一次：列表端点与每次对话都读这一列，不去重会刷屏到根因被淹没。
_DECRYPT_FAILURE_REPORTED = False


class EncryptedText(TypeDecorator):
    """透明的「落库加密、读取解密」文本列（API Key 用）。

    业务代码继续当明文读写，加密只在 bind/result 边界发生；解不开时返回 None——这层拿不到
    行上下文，抛出去会让整个后台 500；要知道原因用 `audit_provider_key_decryption()`。
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001, ANN201
        if value is None:
            return None
        return encrypt_secret(str(value))

    def process_result_value(self, value, dialect):  # noqa: ANN001, ANN201
        if value is None:
            return None
        try:
            return decrypt_secret(str(value))
        except SecretDecryptError:
            global _DECRYPT_FAILURE_REPORTED
            if not _DECRYPT_FAILURE_REPORTED:
                _DECRYPT_FAILURE_REPORTED = True
                logger.error(
                    "有提供商的 API Key 解密失败：AUTH_SECRET 可能已更换。"
                    "受影响的行会以「未配置密钥」呈现；调用 /health/ready 可看到具体是哪几个。",
                    exc_info=True,
                )
            return None


async def audit_provider_key_decryption() -> list[str]:
    """逐条试解 `model_provider.api_key`，返回解不开的提供商 id（十六进制字符串）。

    必须用原始 SQL：经过 `EncryptedText` 后拿到的已是解密结果，分不清「没配」与「解不开」。
    换 `AUTH_SECRET` 是允许的运维动作，其症状指向填错 key，所以要由启动日志与就绪探针说出真相。
    """
    from sqlalchemy import text as _text

    from app.config.secret_box import PREFIX

    async with async_session_maker() as session:
        rows = (await session.execute(_text("SELECT id, api_key FROM model_provider"))).all()
    broken: list[str] = []
    for row in rows:
        raw = row[1]
        if not raw or not str(raw).startswith(PREFIX):
            continue
        try:
            decrypt_secret(str(raw))
        except SecretDecryptError:
            broken.append(str(row[0]))
    return broken


class Base(DeclarativeBase):
    pass


class User(SQLAlchemyBaseUserTableUUID, Base):
    """用户表：fastapi-users 的标准列加上本项目的 `role`。

    `role` 是唯一权限判据（见 `app/roles.py`），只能由管理后台授予；`is_superuser` 只是它的
    派生镜像、由 `apply_role()` 单点写入，鉴权绝不读它，否则同一个问题会有两个答案。
    """

    #: 存成小写字符串而非 Enum：加档位只改 sql/init.sql，不必在 PG 维护类型。取值见 `app/roles.py`。
    role: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=ROLE_USER,
        server_default=ROLE_USER,
    )

    @property
    def role_level(self) -> Role:
        """解析后的角色等级。非法值时降级为 USER（见 `Role.parse`）。"""
        return Role.parse(self.role)


class AccessToken(SQLAlchemyBaseAccessTokenTableUUID, Base):
    """会话表：`token` / `user_id` / `created_at`；`user_id` 级联，删用户即清会话行。

    `token` 存的是 SHA-256 的 base64url 摘要而不是 cookie 原文，库被读也无法直接回放。
    """


def hash_access_token(token: str) -> str:
    """会话 token → 入库值（SHA-256 base64url，43 字符，正好卡进基表的 `String(43)`）。"""
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class HashedAccessTokenDatabase(SQLAlchemyAccessTokenDatabase):
    """写入摘要、按摘要查询，兼容历史明文行，并用 Redis 做 cache-aside 热缓存。

    库是权威：撤销先删库再删缓存，进程崩溃时的缓存残留由短 TTL（默认 60s）兜住，
    撤销的传播延迟因此有界。键是摘要而非原始 token，Redis 被读也拿不到可回放的令牌。
    """

    async def get_by_token(self, token: str, max_age=None):  # noqa: ANN001, ANN201
        hashed = hash_access_token(token)
        cached = await _session_cache_get(hashed)
        if cached is not None:
            return cached

        row = await super().get_by_token(hashed, max_age)
        if row is None:
            # 兼容升级前写入的明文行：无法提前哈希成键，故只回源不回填缓存。
            return await super().get_by_token(token, max_age)
        await _session_cache_set(hashed, row)
        return row

    async def delete(self, access_token) -> None:  # noqa: ANN001
        # 缓存命中的行是临时构造、不在任何会话里的实例，上游 `session.delete()` 对它
        # 会抛 InvalidRequestError（登出 500），所以统一按主键删。
        token = str(access_token.token)
        await self.session.execute(delete(self.access_token_table).where(self.access_token_table.token == token))
        await self.session.commit()
        # 先库后缓存：缓存删除失败时，TTL 到期后仍会回源到已删除的行。
        await _session_cache_delete(token)


async def _session_cache_get(hashed_token: str) -> AccessToken | None:
    """从 Redis 取会话行；未启用/未命中/任何故障都返回 None，即回源数据库。

    刻意不抛异常：认证路径上多一个可失败的依赖，等于把 Redis 抖动升级成全站 401。
    """
    if not settings.redis_session_cache_enabled:
        return None
    from app.services.redis_client import get_redis

    try:
        raw = await get_redis().get(f"{settings.redis_key_prefix}:session:{hashed_token}")
    except Exception:  # noqa: BLE001 - 缓存故障绝不阻塞认证
        logger.warning("读取会话缓存失败，回源数据库", exc_info=True)
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        return AccessToken(
            token=hashed_token,
            user_id=uuid.UUID(payload["user_id"]),
            created_at=datetime.fromisoformat(payload["created_at"]),
        )
    except (ValueError, KeyError, TypeError):
        # 结构不认识（版本升级/别的写者）：当作 miss，并让键尽快过期
        logger.warning("会话缓存内容无法解析，已忽略")
        return None


async def _session_cache_set(hashed_token: str, row) -> None:  # noqa: ANN001
    """把会话行写进 Redis，TTL 取「缓存上限」与「会话剩余寿命」的较小值。

    缓存不能比会话本身活得更久，否则已过期的会话还能从缓存里被取出来。
    """
    if not settings.redis_session_cache_enabled:
        return
    ttl = _session_cache_ttl(row.created_at)
    if ttl <= 0:
        return
    from app.services.redis_client import get_redis

    payload = json.dumps(
        {"user_id": str(row.user_id), "created_at": row.created_at.isoformat()},
        separators=(",", ":"),
    )
    try:
        await get_redis().set(
            f"{settings.redis_key_prefix}:session:{hashed_token}",
            payload,
            ex=ttl,
        )
    except Exception:  # noqa: BLE001 - 回填失败只影响下一次读的性能
        logger.warning("写入会话缓存失败（不影响本次请求）", exc_info=True)


async def _session_cache_delete(hashed_token: str) -> None:
    """删除会话缓存。失败只告警：库已经是权威状态，缓存会随 TTL 过期。"""
    if not settings.redis_session_cache_enabled:
        return
    from app.services.redis_client import get_redis

    try:
        await get_redis().delete(f"{settings.redis_key_prefix}:session:{hashed_token}")
    except Exception:  # noqa: BLE001
        logger.warning("删除会话缓存失败（该会话最迟在缓存 TTL 后失效）", exc_info=True)


def _session_cache_ttl(created_at: datetime) -> int:
    """缓存剩余有效秒数：min(配置上限, 会话剩余寿命)。"""
    lifetime = settings.session_lifetime_seconds
    if lifetime <= 0:
        return 0
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    remaining = int((created_at + timedelta(seconds=lifetime) - datetime.now(timezone.utc)).total_seconds())
    return max(0, min(int(settings.redis_session_cache_ttl_seconds), remaining))


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


class SyncCounter(Base):
    """每用户单调递增的同步版本号（跨设备同步的游标来源），由服务端发号。

    发号走 `INSERT ... ON CONFLICT DO UPDATE ... RETURNING version` 一条语句，行锁保证并发 push
    不会拿到同一个号；必须与业务写入同事务（否则出现「版本前进、行还没写」的空洞），时间戳不可替代。
    """

    __tablename__ = "sync_counter"

    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("user.id", ondelete="CASCADE"),
        primary_key=True,
    )
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class Conversation(Base):
    """同步用的会话行（浏览器 Dexie 里 `sessions` 的服务端镜像）。

    主键由客户端生成（`sess_<uuid>`）：服务端发主键会让重试变成多一行；归属只由 `user_id` 判定。
    消息 `seq` 取 `seq_counter` 而非 `version`（排序会跳位）；`deleted` 是墓碑，硬删除会让会话复活。
    """

    __tablename__ = "conversation"
    __table_args__ = (Index("ix_conversation_pull", "user_id", "version"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: 客户端自己的修订号：服务端据此拒绝「旧内容覆盖新内容」的重放。
    client_rev: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 消息顺序发号器（每个会话独立，只增不减）。
    seq_counter: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 拉取游标用的服务端版本号，任何一行变化都会取新号。
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: 客户端毫秒时间戳，仅用于展示排序（不可作为同步判据）。
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class Message(Base):
    """同步用的消息行（浏览器 Dexie 里 `messages` 的服务端镜像）。

    前端私有结构（toolCalls / clarification / usage …）整包存 JSONB：拆成列等于前端每改一次结构
    都要改建库脚本。`seq` 只在插入时由服务端赋值、之后不变，是同一会话内跨设备顺序的唯一依据。
    """

    __tablename__ = "message"
    __table_args__ = (
        Index("ix_message_pull", "user_id", "version"),
        Index("ix_message_conversation", "conversation_id", "seq"),
        UniqueConstraint("conversation_id", "seq", name="uq_message_conversation_seq"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("conversation.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: 与 conversation.user_id 冗余一份：拉取时不必 join，索引能直接命中。
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    client_rev: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


# ---------------------------------------------------------------------
# 引擎与会话
# ---------------------------------------------------------------------
DATABASE_URL = settings.database_url


def _engine_kwargs() -> dict[str, object]:
    """引擎参数。单独成函数是为了让测试能构造同参数的独立引擎。

    `pool_pre_ping` 挡住死连接（重启/断连），否则是偶发 500；`application_name` 供库里区分本服务。
    `statement_timeout` 让服务端真取消超时查询：`wait_for` 只放弃等待，烂查询仍占连接。
    """
    from sqlalchemy.pool import NullPool

    server_settings = {"application_name": "bennett-agent"}
    if settings.database_statement_timeout_ms > 0:
        server_settings["statement_timeout"] = str(settings.database_statement_timeout_ms)

    if settings.database_pool_mode == "null":
        return {
            "poolclass": NullPool,
            "pool_pre_ping": False,
            "connect_args": {
                "server_settings": server_settings,
                "timeout": settings.database_connect_timeout,
            },
        }

    return {
        "pool_size": settings.database_pool_size,
        "max_overflow": settings.database_max_overflow,
        "pool_timeout": settings.database_pool_timeout,
        "pool_recycle": settings.database_pool_recycle,
        "pool_pre_ping": True,
        "connect_args": {
            "server_settings": server_settings,
            # 建连超时（asyncpg 默认 60s）：数据库不可达时不该让请求挂一分钟
            "timeout": settings.database_connect_timeout,
        },
    }


engine = create_async_engine(DATABASE_URL, **_engine_kwargs())  # type: ignore[arg-type]
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


def build_engine(url: str | None = None):  # noqa: ANN201 - 返回值是 AsyncEngine
    """按当前配置（或指定 URL）构造一个引擎。测试与运维脚本用。"""
    return create_async_engine(url or DATABASE_URL, **_engine_kwargs())  # type: ignore[arg-type]


#: schema 唯一事实来源：整段幂等，可直接重复执行。
INIT_SQL_PATH = PROJECT_ROOT / "sql" / "init.sql"


def init_sql_text() -> str:
    return INIT_SQL_PATH.read_text(encoding="utf-8")


async def verify_schema_at_head() -> None:
    """执行 sql/init.sql 建表，然后逐列比对模型与库，漂移就拒绝启动。

    `CREATE TABLE IF NOT EXISTS` 对既有表是 no-op，新列永远建不出来，而启动照样成功，直到某条
    冷门路径 SELECT 才 500（CI 是空库，测试抓不到）；故比对后给出可复制的 ALTER TABLE，宁可起不来。
    """
    if not settings.database_apply_schema:
        logger.warning("已跳过建库脚本（DATABASE_APPLY_SCHEMA=false）")
        return
    # SQLAlchemy 的 text() 走预处理语句，一次只能一条命令；这里逐句执行。
    statements = [s.strip() for s in init_sql_text().split(";") if s.strip()]
    async with engine.begin() as connection:
        for statement in statements:
            await connection.execute(text(statement))

    missing = await _missing_columns()
    if missing:
        detail = "\n".join(f"  ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column};" for table, column in missing)
        raise RuntimeError(
            "数据库结构已漂移：模型里有这些列，库里没有——`sql/init.sql` 的 "
            "CREATE TABLE IF NOT EXISTS 对既有表不会加列。请重建开发库"
            "（docker compose down -v && docker compose up -d postgres），"
            "或在生产上手工执行：\n" + detail
        )
    logger.info("数据库结构已就绪")


async def _missing_columns() -> list[tuple[str, str]]:
    """模型有、库里没有的列，返回 `[(表名, 列名)]`。

    只查缺列不查多列：历史遗留的多列不影响正确性，而拒绝启动的代价远高于一次运行期 500。
    """
    from sqlalchemy import inspect as _inspect

    missing: list[tuple[str, str]] = []

    def compare(sync_connection):  # noqa: ANN001, ANN202
        inspector = _inspect(sync_connection)
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                missing.append((table.name, "<整张表缺失>"))
                continue
            existing = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name not in existing:
                    missing.append((table.name, column.name))

    async with engine.connect() as connection:
        await connection.run_sync(compare)
    return missing


# ---------------------------------------------------------------------
# 会话维护
# ---------------------------------------------------------------------
async def cleanup_expired_access_tokens(lifetime_seconds: int) -> int:
    """删除已过期的会话行，返回删除条数。

    `DatabaseStrategy.read_token` 只把 `created_at` 当过滤条件、从不删行，没有这个维护动作
    表会随登录次数无限增长。用独立会话：调用方通常正处于自己的事务里，这是无关的维护动作。
    """
    if lifetime_seconds <= 0:
        # 与 services/auth.py 的 get_database_strategy 同源：lifetime<=0 时 cutoff 落在现在或未来，
        # 会把全部有效会话当成过期删掉，比不清理危险得多。
        raise ValueError(
            f"cleanup_expired_access_tokens 的 lifetime_seconds 必须 > 0，当前为 {lifetime_seconds}："
            "0/负值会让服务端会话永不过期，按此清理会误删全部有效会话。"
        )

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=lifetime_seconds)
    async with async_session_maker() as session:
        stale = (await session.execute(select(AccessToken.token).where(AccessToken.created_at < cutoff))).scalars()
        tokens = list(stale)
        await session.execute(delete(AccessToken).where(AccessToken.created_at < cutoff))
        await session.commit()

    # 缓存与库一起清：只删库时，已登出的会话还能从 Redis 里取出来用（最坏到 TTL）。
    for token in tokens:
        await _session_cache_delete(token)
    return len(tokens)


async def delete_user_access_tokens(user_id: uuid.UUID) -> int:
    """删除某个用户的全部会话行，返回删除条数（改密 / 踢人 / 停用账号用）。

    只按 `user_id` 过滤，否则改 A 的密码会把 B 踢下线。必须先读出摘要再删：缓存键就是摘要，
    不知道键名撤销就拖到一个 TTL 才生效。每用户几十条顶天，一次全取不成为负担。
    """
    async with async_session_maker() as session:
        tokens = list(
            (await session.execute(select(AccessToken.token).where(AccessToken.user_id == user_id))).scalars()
        )
        result = await session.execute(delete(AccessToken).where(AccessToken.user_id == user_id))
        await session.commit()

    for token in tokens:
        await _session_cache_delete(token)
    return result.rowcount or 0


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session


async def get_user_db(session: AsyncSession = Depends(get_async_session)):  # noqa: B008
    yield SQLAlchemyUserDatabase(session, User)


async def get_access_token_db(session: AsyncSession = Depends(get_async_session)):  # noqa: B008
    yield HashedAccessTokenDatabase(session, AccessToken)


__all__ = [
    "DATABASE_URL",
    "AccessToken",
    "AppSetting",
    "Base",
    "Conversation",
    "HashedAccessTokenDatabase",
    "Message",
    "ModelProvider",
    "ProviderModel",
    "SyncCounter",
    "User",
    "async_session_maker",
    "build_engine",
    "cleanup_expired_access_tokens",
    "delete_user_access_tokens",
    "engine",
    "get_access_token_db",
    "get_async_session",
    "get_user_db",
    "hash_access_token",
    "verify_schema_at_head",
]


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
