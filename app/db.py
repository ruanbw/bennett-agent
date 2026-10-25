"""数据库基础设施：异步引擎、ORM 基类，以及业务表定义。

## 为什么是 Postgres（而不是 SQLite）

早期版本用 SQLite：单进程、写入压力低，WAL 模式足够。换库的触发点不是性能，而是
**部署形态**——一旦要多实例（水平扩容、滚动发布、worker 与服务分离），本地文件库就
不再成立：每个实例各写各的，会话、限流、同步游标全都对不上。

换到 Postgres 之后，那些为 SQLite 打的补丁也一并撤掉了：`PRAGMA foreign_keys`
（PG 默认强制外键）、`journal_mode` 检查、文件权限收紧、手写 `ALTER TABLE` 迁移。
后者的替代品是 Alembic：**结构变更有版本、可回滚、可在部署流水线里先于应用执行**。

## 结构变更的唯一入口是 Alembic

`migrations/versions/` 是权威历史，应用启动时**不建表、不改表**。它只做一件事：
校验库里的 revision 是否等于 head（`verify_schema_at_head`），不等就拒绝启动。

为什么不在启动时自动 `upgrade head`：滚动发布时，先起来的实例会把结构升到新版本，
而其他实例还在跑旧代码。那个窗口里的错误是随机的（旧查询撞新列、新列还没回填），
比「明确拒绝启动」难查一个数量级。

## 各组表的职责

- `user`：fastapi-users 的标准用户表（邮箱 + 密码哈希 + 三个布尔位）。
  `is_active=False` 就是「封禁」：认证依赖要求 active=True，且封禁时会删掉该用户的
  全部会话行，所以在途请求与刷新页面都会立刻失效。
- `access_token`：**服务端会话**。一条记录 = 一个登录态。这是「不用 JWT」的落点：
  token 只是一串随机值，状态在库里，因此登出/踢人/改密失效都是删一行，立即生效。
- `model_provider` / `provider_model` / `model_price`：管理后台维护的模型清单与计价。
- `sync_counter` / `conversation` / `message`：跨设备同步的服务端镜像。
- `knowledge_document` / `knowledge_chunk`：知识库（RAG）。
- `uploaded_file`：上传文件的原件元数据与解析状态（原件本身在对象存储里）。

## 关于 user 表名

`user` 是 SQL 保留字。SQLAlchemy 与 Alembic 都会给它加双引号，因此可用；但手写 SQL
（运维排查时）必须写 `"user"`，否则 Postgres 会把它解析成 `current_user`——那是个函数，
报错信息（`syntax error at or near "user"`）不会提示你少了一对引号。
"""

import base64
import hashlib
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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
    LargeBinary,
    Numeric,
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
from app.config.secret_box import decrypt_secret, encrypt_secret
from app.roles import ROLE_USER, Role

logger = logging.getLogger(__name__)

#: 项目根目录。alembic.ini 与 migrations/ 都锚在这里，**不能用当前工作目录**：
#: 从别的目录启动（IDE、systemd、容器 WORKDIR 变化）会找不到迁移脚本，而报错是
#: 「Can't find script directory」，与真正的原因（路径不对）毫无关系。
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class EncryptedText(TypeDecorator):
    """透明的「落库加密、读取解密」文本列（API Key 用）。

    `TypeDecorator` 让所有业务代码继续把 `api_key` 当明文读写，加密只发生在
    SQLAlchemy 的 bind/result 边界；旧明文行由 `decrypt_secret` 原样返回。
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
        return decrypt_secret(str(value))


class Base(DeclarativeBase):
    pass


class User(SQLAlchemyBaseUserTableUUID, Base):
    """用户表。

    fastapi-users 定义的部分：`id`(UUID) / `email` / `hashed_password` /
    `is_active` / `is_superuser` / `is_verified`。本项目自己加的：`role`。

    - `is_active=False` 是**封禁**：认证依赖要求 active=True，且封禁接口会同时删除
      该用户的全部会话行，因此在途请求与刷新都会立即生效。
    - `role` 是**唯一的权限判据**（见 `app/roles.py`）。账号的来源只有两个：
      播种脚本建出的超管，或公开注册出来的普通用户；**角色只能由管理后台授予**，
      服务里没有任何「注册即管理员」的路径。
    - `is_superuser` 是 `role` 的**派生镜像**（`role >= admin`），由 `apply_role()`
      单点写入。保留它只是因为 fastapi-users 的基表强制带这一列，**鉴权绝不读它**——
      否则同一个问题会有两个答案，而它们迟早不一致。
    """

    #: 存储成小写字符串而不是 Enum：加档位只需一次迁移，不必在 PG 里维护类型。
    #: 取值与等级见 `app/roles.py`。列的默认值由迁移显式声明（`server_default='user'`），
    #: 因为历史行需要一个确定的落点。
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
    """会话表：`token` / `user_id` / `created_at`。

    `user_id` 带 `ondelete="cascade"`，Postgres 默认强制外键，因此删用户会自动清掉
    会话行——这一点比 SQLite 省心（那边要显式 `PRAGMA foreign_keys=ON`，且**测试里
    忘了开就会漏掉整条级联路径**）。

    **`token` 列存的是 SHA-256 的 base64url 摘要，不是 cookie 里的原文**：
    数据库文件泄露/被读时，攻击者拿不到可直接回放的会话令牌（I-10）。
    旧版本写入的明文行仍可读（见 `HashedAccessTokenDatabase` 的兼容回退），
    重新登录后自然换成摘要；无需停机迁移。
    """


def hash_access_token(token: str) -> str:
    """会话 token → 入库值（SHA-256 base64url，43 字符）。

    43 字符正好落在 fastapi-users 基表 `String(43)` 的长度内，因此不用改表结构。
    """
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class HashedAccessTokenDatabase(SQLAlchemyAccessTokenDatabase):
    """写入摘要、按摘要查询；同时兼容历史明文行，并用 Redis 做**热缓存**。

    ## 为什么缓存和数据库都要写

    会话查询是每个请求的必经路径（包括 SSE 与长轮询）。Postgres 上一次主键查找是
    微秒级，但请求量乘上往返延迟之后，它仍然是连接池里最热的那条查询。

    **数据库始终是权威来源**，Redis 只是 cache-aside：

    - 读：命中直接返回；miss 回源数据库并回填；
    - 写（登录）：写库成功后回填；
    - 删（登出/改密/封禁）：先删库，再删缓存。库是权威的，所以**即使删缓存失败**，
      Redis 一旦不可用也会回源到已删除的库行——不会出现「库删了但会话还能用」的
      永久窗口。

    ## 缓存 TTL 很短，这是有意的

    `settings.redis_session_cache_ttl_seconds` 默认 60 秒，而不是会话剩余的 30 天。
    原因：把「撤销」的传播延迟压到一分钟以内。撤销路径会尽力删缓存，但进程崩溃、
    网络分区这类情况下缓存会残留；短 TTL 让最坏情况有界，而不是依赖一次删除成功。
    换来的代价只是每分钟多一次主键查询，可以忽略。

    ## 缓存里不出现原始 token

    键是 `sha256(token)` 的摘要（与库里存的是同一个值），值是 `user_id + created_at`。
    Redis 被读（备份、误开端口、同主机其他进程）也拿不到可回放的会话令牌——与库里
    存摘要的理由完全一致。
    """

    async def get_by_token(self, token: str, max_age=None):  # noqa: ANN001, ANN201
        hashed = hash_access_token(token)
        cached = await _session_cache_get(hashed)
        if cached is not None:
            return cached

        row = await super().get_by_token(hashed, max_age)
        if row is None:
            # 兼容升级前写入的明文行。它们无法提前哈希成键，因此只回源、不回填缓存：
            # 重新登录后自然换成摘要行。
            return await super().get_by_token(token, max_age)
        await _session_cache_set(hashed, row)
        return row

    async def delete(self, access_token) -> None:  # noqa: ANN001
        # 缓存命中的行是 `_session_cache_get` **临时构造的实例**，不在任何会话里；
        # 上游的 `session.delete()` 对这种实例会抛 InvalidRequestError「not persisted」，
        # 表现为登出 500（同一次登出里认证依赖先查一次并回填缓存，destroy_token 再查
        # 必然命中缓存）。所以统一按主键删，兼容「缓存命中」与「回源行」两种来源。
        token = str(access_token.token)
        await self.session.execute(
            delete(self.access_token_table).where(self.access_token_table.token == token)
        )
        await self.session.commit()
        # 先库后缓存：缓存删除失败时，TTL 到期后仍会回源到已删除的行。
        await _session_cache_delete(token)


async def _session_cache_get(hashed_token: str) -> AccessToken | None:
    """从 Redis 取会话行；未启用/未命中/任何 Redis 故障都返回 None（回源数据库）。

    这里**刻意不抛异常**：认证路径上多一个可能失败的依赖，等于把「Redis 抖动」
    升级成「全站 401」。降级是可观测的（日志 + 指标），不是静默的。
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

    剩余寿命按 `created_at + session_lifetime` 算：缓存不能比会话本身活得更久，
    否则一个已过期的会话还能从缓存里被取出来。
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
    """模型提供商：一个可独立寻址的 OpenAI 兼容端点。

    `base_url` / `api_key` 是**运行时**凭据：管理后台可以接入多个提供商（本机 LiteLLM
    网关、官方 API、自建 vLLM……），每个提供商用自己的凭据与模型清单。

    `slug` 是生成一次、之后不随改名变化的稳定标识——它参与对外模型路由键
    (`{slug}/{model_id}`)，改名不能把用户已保存的模型选择悄悄改掉。

    **API key 落库加密**（`EncryptedText`，密钥来自 `AUTH_SECRET`）。对外 API 永远只
    返回掩码（见 `app/schemas/admin.py:mask_api_key`）。更强的隔离应改用密钥管理服务，
    而不是在这里再加一层自造加密。
    """

    __tablename__ = "model_provider"

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    slug: Mapped[str] = mapped_column(String(60), nullable=False, unique=True, index=True)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    api_key: Mapped[str | None] = mapped_column(EncryptedText, nullable=True)
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
    #: JSONB 而不是 JSON：PG 下它会解析成二进制结构，可以建表达式索引，
    #: 也避免每次读取都重新解析一遍文本。
    supported_reasoning_levels: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
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


class SyncCounter(Base):
    """每用户单调递增的同步版本号（跨设备同步的游标来源）。

    版本号由**服务端**发号，客户端只保存一个不透明游标。三件事必须由它保证：

    1. 单调：`version` 只增不减，任何一行发生变化都取一个新号；
    2. 单调的来源可原子化：`INSERT ... ON CONFLICT DO UPDATE SET version = version + 1
       RETURNING version` 是一条语句。Postgres 会在该行上加行锁，因此并发的两次 push
       不会拿到同一个号（SQLite 靠「只允许一个写者」达到同样效果，代价是整个库串行）；
    3. 与业务写入同事务：发号与写行必须一起提交，否则客户端可能拉到
       「版本号已前进、行还没写」的空洞。

    时间戳不能替代它：客户端时钟跨设备不可比，服务端时间戳在同一毫秒也会重复。
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

    主键是**客户端生成的** `sess_<uuid>`：同步接口必须幂等，而服务端发主键会让
    重试变成「每次重试都多一行」。id 全局唯一（uuid v4），不属于某个用户，「是不是
    我的」由 `user_id` 判定——所有查询都必须带上它。

    `seq_counter` 是消息顺序的发号器：消息的 `seq` 从它取，保证同一个会话内的先后
    由服务端决定（客户端时钟不可信）。之所以不复用 `version`：`version` 会随每次修改
    前进（流式中的助手消息每完成一次推送都会变），拿它排序会让消息在列表里跳位。

    `deleted` 是墓碑：删会话必须让其他设备也能删掉，硬删除会让「删掉的会话」在下一
    次推送时复活。墓碑行永久保留（每行几十字节），消息由外键级联或显式删除清理。
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

    前端消息形状里 `toolCalls / clarification / fallback / usage / error / partOrder`
    都是前端私有结构。**整包存 JSONB** 而不是逐列建模：拆成列等于以后前端每改一次
    结构就要一次数据库迁移。服务端只需要能校验的东西（归属、顺序、大小），
    不需要理解聊天语义。

    `seq` 在**插入时**由服务端赋值并且之后不再变（改内容只前进 `version`，不动 seq），
    这是「同一会话内跨设备看到同一顺序」的唯一依据。
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


class ModelPrice(Base):
    """模型的计价信息（每百万 token 的输入/输出价格）。

    ## 为什么是独立表而不是给 `provider_model` 加两列

    价格是**独立生命周期**的数据：它可以缺（未知模型）、会变（上游调价）、也可能
    由运维批量导入。混进模型元数据表会让「同步上游模型列表」这条路径有覆盖它的
    风险，而价格被同步动作悄悄清掉是最难发现的一类账目错误。

    ## 单位与精度

    `*_per_mtok` 是「每百万 token 的价格」，单位是**计价货币**（`currency`，默认 USD）。
    用百万为单位而不是每 token：后者是 1e-6 量级的小数，浮点误差与展示都难处理。

    `Decimal` 而不是 `Float`：钱不能有二进制浮点误差。PG 的 NUMERIC 是精确十进制，
    SQLAlchemy 会经 `Decimal` 往返——累加几百次仍然精确。

    ## 为什么允许为空

    绝大多数自建网关与本地模型（vLLM、LiteLLM 本地代理）没有公开计价，或者部署方
    根本不在乎这个数。缺价格时前端显示「未配置价格」并**不显示金额**——显示 0 会被
    读成「免费」，那是一个明确的错误结论。
    """

    __tablename__ = "model_price"
    __table_args__ = (UniqueConstraint("model_id", name="uq_model_price_model"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    #: 对外模型键（`{provider.slug}/{model_id}`），与 `provider_model` 的对外键一致。
    #:
    #: 刻意**不做外键**：价格表要能提前录入还没启用的模型，也要能在模型被删除后保留
    #: 历史账目。外键会同时挡住这两件事（删模型时 CASCADE 掉价格，历史就没了）。
    model_id: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    #: 每百万输入 token 的价格；None = 未知。
    input_per_mtok: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    #: 每百万输出 token 的价格；None = 未知。
    output_per_mtok: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    #: 每百万缓存命中输入 token 的价格；None = 「与普通输入同价」（多数厂商如此）。
    cached_input_per_mtok: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class KnowledgeDocument(Base):
    """知识库文档（RAG 的入库单位）。

    `dimensions` 与 `embedding_model` 存在**文档**上而不是全局配置里：检索时要按
    片段的维度筛掉不兼容的候选，而「换了 embedding 模型却没重建索引」是这类系统最
    常见的静默故障——旧向量还在，与查询算出来的相似度只是噪声，检索却仍会「成功
    返回」若干条结果。
    """

    __tablename__ = "knowledge_document"
    __table_args__ = (Index("ix_knowledge_document_user", "user_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    #: 来源说明（文件名、URL）。展示用，不参与检索。
    source: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: 生成这些向量时用的模型。换模型后旧文档必须重建，否则维度对不上。
    embedding_model: Mapped[str] = mapped_column(String(200), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )


class KnowledgeChunk(Base):
    """知识库片段（检索的最小单位）。

    向量存成 float32 的二进制（`app/services/rag/store.py:pack_vector`）而不是
    JSON：1024 维的 JSON 文本约 12KB，二进制是 4KB，且读出来不用 `json.loads`。
    多文档检索时这个差别就是几十毫秒。

    为什么不用 pgvector：当前检索是「按用户取全量片段，在 Python 里算余弦 + 词法
    加权」。片段规模在万级以内时这是几十毫秒的事，而 pgvector 会引入一个**必须由
    数据库镜像预装**的扩展（`CREATE EXTENSION vector` 在多数托管 PG 上需要单独开通）。
    规模上去之后再换：`embedding` 列改成 `vector(N)` + HNSW 索引，混合检索的权重
    逻辑可以保留。

    `user_id` 与 `document_id` 冗余一份：检索按用户过滤时不必 join 文档表，
    索引能直接命中（与 `message.user_id` 同一条取舍）。
    """

    __tablename__ = "knowledge_chunk"
    __table_args__ = (
        Index("ix_knowledge_chunk_user", "user_id"),
        Index("ix_knowledge_chunk_document", "document_id", "chunk_index"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("knowledge_document.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    #: 所属章节标题（最近的 Markdown 标题）；展示「这句来自哪一节」用。
    heading: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    #: float32 小端二进制（`array.array("f")` 的 tobytes）。
    embedding: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)


# ---------------------------------------------------------------------
# 上传文件
# ---------------------------------------------------------------------
#: 排队中，等待 worker 取走。
UPLOAD_STATUS_PENDING = "pending"
#: 已被某个 worker 取走（带租约，见 `processing_started_at`）。
UPLOAD_STATUS_PROCESSING = "processing"
#: 原件已存入对象存储、文本已解析、片段已写入知识库。
UPLOAD_STATUS_READY = "ready"
#: 重试次数用尽或遇到不可重试的错误（格式损坏、加密 PDF…）。
UPLOAD_STATUS_FAILED = "failed"

UPLOAD_STATUSES = frozenset(
    {UPLOAD_STATUS_PENDING, UPLOAD_STATUS_PROCESSING, UPLOAD_STATUS_READY, UPLOAD_STATUS_FAILED}
)


class UploadedFile(Base):
    """上传文件的原件元数据与解析状态。

    原件在对象存储里（`storage_key`），**库里不存二进制**：把 PDF 塞进 BYTEA 会让
    每次备份、每条 `SELECT *`、每个连接都拖着它们走。库里只留「去哪儿找 + 处理到哪一步」。

    ## 状态机

        pending → processing → ready
                    ↓ 失败且还有重试次数
                  pending
                    ↓ 次数用尽 / 不可重试
                  failed

    `processing` 带租约（`processing_started_at`）：worker 崩溃后行会永久卡在该状态，
    所以取任务的查询会把「租约过期的 processing」重新当作可领取。没有这一步，
    一次 OOM Kill 就能让某个用户的文件永远停在「解析中」。

    ## 去重

    `(user_id, sha256)` 唯一：同一用户重复上传同一份文件不会产生第二个原件、
    第二份切片，也不会烧第二次 embedding 配额。跨用户**不共享**——知识库是私有的，
    共享原件会把「谁传过什么」泄露成 timing 侧信道。
    """

    __tablename__ = "uploaded_file"
    __table_args__ = (
        Index("ix_uploaded_file_user", "user_id", "created_at"),
        # worker 的取任务查询走这个索引：WHERE status=... ORDER BY created_at
        Index("ix_uploaded_file_queue", "status", "created_at"),
        UniqueConstraint("user_id", "sha256", name="uq_uploaded_file_user_sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: 用户提供的原始文件名（**只用于展示**，绝不参与对象键构造）。
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: 嗅探/用户声明的 MIME，供解析器选择与展示。
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: 原件内容的 SHA-256（hex），用于去重与完整性校验。
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 对象存储里的键：`{prefix}/{user_id}/{uuid}`，不含扩展名与用户文件名。
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=UPLOAD_STATUS_PENDING)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 失败原因（给用户看的一句话）与内部细节（日志里看）。
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: 租约起点。worker 每处理一份就写一次，用于判断「是不是被崩溃的 worker 拿走了」。
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: 解析成功后指向的知识库文档。删除文档时置空（原件仍保留，可重新解析）。
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID,
        ForeignKey("knowledge_document.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------
# 引擎与会话
# ---------------------------------------------------------------------
DATABASE_URL = settings.database_url


def _engine_kwargs() -> dict[str, object]:
    """引擎参数。单独成函数是为了让测试能构造同参数的独立引擎。

    `pool_pre_ping`：Postgres 重启、pgbouncer 空闲断连、云厂商的静默回收都会让池里
    留下死连接。没有 pre-ping 时第一个拿到死连接的请求报 `ConnectionDoesNotExist`，
    而它看起来像「偶发的 500」。代价是每次从池里取连接多一次往返——比偶发 500 便宜。

    `server_settings` 随连接下发：
    - `application_name` 让 `pg_stat_activity` 里能区分本服务与其他客户端，排查锁
      等待时是必需品（否则只有一串 `asyncpg`）；
    - `statement_timeout` 让**服务端**真正取消超时查询。应用层 `wait_for` 只是放弃
      等待，查询还在库里跑并占着连接——连接池被一条烂查询耗尽是最常见的雪崩起点。
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


# ---------------------------------------------------------------------
# Alembic：版本校验（不做自动迁移）
# ---------------------------------------------------------------------
def _alembic_config():  # noqa: ANN202 - alembic.config.Config
    """加载 alembic.ini。路径锚定项目根，不依赖当前工作目录。"""
    from alembic.config import Config

    return Config(str(PROJECT_ROOT / "alembic.ini"))


def expected_head_revision() -> str:
    """迁移脚本里的 head revision（纯文件读取，不连库）。"""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(_alembic_config())
    head = script.get_current_head()
    if head is None:
        raise RuntimeError("migrations/versions 里没有任何迁移脚本：数据库结构无从建立。")
    return head


async def current_revision() -> str | None:
    """库里的当前 revision；`alembic_version` 表不存在时返回 None。"""
    from alembic.runtime.migration import MigrationContext

    def read(connection) -> str | None:  # noqa: ANN001 - run_sync 要求同步函数
        return MigrationContext.configure(connection).get_current_revision()

    async with engine.connect() as connection:
        return await connection.run_sync(read)


async def verify_schema_at_head() -> None:
    """启动自检：库结构必须与迁移脚本的 head 一致，否则拒绝启动。

    为什么不自动升级、也不只是打个警告：

    - 自动升级：滚动发布时先起来的实例会把结构推到新版本，而旧代码还在跑。错误
      变成随机的（旧查询撞新列），最难排查的那一类。
    - 只警告：结构不匹配的表现是运行期某条查询报 `UndefinedColumn`，用户先发现。
      这里是纯配置/部署问题，启动时拒绝比线上猜快得多。

    本地开发想省一步可以设 `DATABASE_REQUIRE_HEAD_REVISION=false`——那等于承认
    「我在拿旧结构跑新代码」，仅限本机。
    """
    head = expected_head_revision()
    current = await current_revision()
    if current == head:
        logger.info("数据库结构已是最新: revision=%s", head)
        return

    detail = f"数据库结构不是最新：当前 revision={current or '（无，未执行过迁移）'}，代码期望 {head}。"
    hint = "请先执行迁移：docker compose run --rm migrate（或 alembic upgrade head）。"
    if settings.database_require_head_revision:
        raise RuntimeError(f"{detail} {hint}")
    logger.warning("%s 已按配置继续启动（DATABASE_REQUIRE_HEAD_REVISION=false）。", detail)


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
    启动清理）通常正处于自己的事务里，清理是与该请求无关的维护动作。
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
        stale = (await session.execute(select(AccessToken.token).where(AccessToken.created_at < cutoff))).scalars()
        tokens = list(stale)
        await session.execute(delete(AccessToken).where(AccessToken.created_at < cutoff))
        await session.commit()

    # 缓存与库一起清：只删库时，已登出的会话还能从 Redis 里取出来用（最坏到 TTL）。
    for token in tokens:
        await _session_cache_delete(token)
    return len(tokens)


async def delete_user_access_tokens(user_id: uuid.UUID) -> int:
    """删除某个用户的**全部**会话行，返回删除条数（改密 / 踢人 / 停用账号用）。

    只按 `user_id` 过滤：同一部署下改 A 的密码不能把 B 踢下线。

    先把 token 摘要读出来再删：Redis 缓存是按摘要建的键，不知道键名就没法让撤销
    立即生效。会话数量在这张表的量级下很小（每用户几十条顶天），一次全取不会成为
    负担；反过来，省略这一步会让「改密后旧会话仍能用一个 TTL」变成安全缺陷。
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
    "UPLOAD_STATUSES",
    "UPLOAD_STATUS_FAILED",
    "UPLOAD_STATUS_PENDING",
    "UPLOAD_STATUS_PROCESSING",
    "UPLOAD_STATUS_READY",
    "AccessToken",
    "AppSetting",
    "Base",
    "Conversation",
    "HashedAccessTokenDatabase",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "Message",
    "ModelPrice",
    "ModelProvider",
    "ProviderModel",
    "SyncCounter",
    "UploadedFile",
    "User",
    "async_session_maker",
    "build_engine",
    "cleanup_expired_access_tokens",
    "current_revision",
    "delete_user_access_tokens",
    "engine",
    "expected_head_revision",
    "get_access_token_db",
    "get_async_session",
    "get_user_db",
    "hash_access_token",
    "verify_schema_at_head",
]


class SandboxLease(Base):
    """一台沙箱机器的租约（用户 × 会话 × 一台机器）。

    ## 为什么这张表必须有，尽管服务端「不保存会话」

    会话历史仍然由客户端回放，但**机器是服务端独有的长生命周期资源**：它上面有什么文件、
    跑了什么进程，客户端不可能知道、也不可能重建。所以这是本项目第一份「服务端唯一真相」
    的运行时状态，落在库里而不是内存里——多 worker 部署下内存态必然对不上。

    ## 为什么是「一个用户一台」

    `user_id` 上的**部分唯一索引**（仅对活动状态生效）同时干两件事：它是「每用户至多
    一台」的强制点，也是并发创建的去重机制——agent 在一轮里并发调用两个沙箱工具是
    真实存在的，没有它就会建出两台机器而只记一条租约。

    曾经按 `(user_id, session_id)` 建索引（一会话一台），后来改成按用户：切换会话不该
    让刚才装好的依赖消失，一个人也不该同时占着两三台。`session_id` 因此只保留
    「最后是哪个会话在用」这条归因信息，不参与寻址。

    索引只覆盖活动状态（`provisioning/ready/paused`），所以历史行可以无限堆积，
    同一用户重建环境也不会撞唯一约束。

    ## 状态机

        provisioning → ready → destroying → destroyed
                   ↘ failed            ↘ destroy_failed

    迁移**必须**用条件 UPDATE（`WHERE state='ready' RETURNING id`）抢占，不能先读后写：
    回收循环和一条正在跑的长任务会同时在场，先读后写会让回收把机器从长任务下面抽走。
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
    #: 最后使用这台机器的会话标识 `sess_<uuid>`（归因用，**不参与寻址**）。
    #: 机器按用户分配，所以一个用户跨会话共用一台；复用时会改写这一列。
    #: **不是身份**：身份始终来自服务端注入的作用域（`app/tools/sandbox_scope.py`）。
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
    #: 最后一次「服务端观察到的工作」（exec / 文件读写）。与心跳分开：
    #: 心跳证明**客户端还在**，它在 Redis 里带 TTL；这个证明**机器还在被用**，
    #: 于是「用户关掉页面但 agent 还在跑长任务」不会被误回收。
    last_activity: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: 绝对上限。只靠空闲回收的话，一个挂着不动的标签页可以永远付费占机。
    hard_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    destroyed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: idle / hard_ttl / session_deleted / user_banned / user_reset / error
    destroy_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: 销毁前导出的产物（对象存储键）。产物比机器重要：机器可以重建，用户的计算结果不行。
    artifact_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: 最近一次失败原因（给运维看，不进模型上下文）。
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class SandboxCommand(Base):
    """沙箱内的每一条命令（审计 + 排障）。

    **不参与任何金额计算**：本项目的计量只有 token（见设计文档 §8），
    沙箱的成本只通过「exec 输出进上下文」这一条通路体现。这张表存在的理由是
    「边界审批 + 审计」模型：既然不再逐条命令打断用户，就必须事后能查清跑过什么。

    `output_head` 存的是**渲染给模型看的同一份截断文本**（不是原始输出）：
    审计要回答的是「模型当时看到了什么」，原始输出既存不下也不该存
    （可能是用户数据）。完整输出留在沙箱里，随机器一起销毁。
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
