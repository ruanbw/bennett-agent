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
from fastapi_users_db_sqlalchemy.generics import GUID
from sqlalchemy import (
    JSON,
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
    event,
    text,
)
from sqlalchemy import Uuid as SQLUuid
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import settings
from app.roles import ALL_ROLES, ROLE_SUPERADMIN, ROLE_USER, Role

logger = logging.getLogger(__name__)


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

    #: 存储成小写字符串而不是 Enum：SQLite 里是 TEXT，加档位不用改表结构
    #: （本项目没有 Alembic）。取值与等级见 `app/roles.py`。
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

    `user_id` 带 `ondelete="cascade"`，但 SQLite 默认**不启用**外键约束，
    所以下面必须显式 `PRAGMA foreign_keys=ON`，否则删用户后会话记录会残留。
    """


class ModelProvider(Base):
    """模型提供商：一个可独立寻址的 OpenAI 兼容端点。

    `base_url` / `api_key` 是**运行时**凭据：管理后台可以接入多个提供商（本机 LiteLLM
    网关、官方 API、自建 vLLM……），每个提供商用自己的凭据与模型清单。

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


#: 同步表的外键列必须用 `GUID` 而不是 `SQLUuid`。
#:
#: fastapi-users 的 user 表用的是它自己的 `GUID` 类型：SQLite 下存成 **CHAR(36) 带横线**
#: 的字符串，而 SQLAlchemy 的 `Uuid` 存成 CHAR(32) 不带横线。两者都是 32 个 hex 字符的
#: uuid，但字面量不同，外键约束会直接拒绝——症状是 push 报 500
#: `FOREIGN KEY constraint failed`，而单元测试因为没开 PRAGMA foreign_keys 反而全绿。
SYNC_USER_FK = GUID


class SyncCounter(Base):
    """每用户单调递增的同步版本号（跨设备同步的游标来源）。

    版本号由**服务端**发号，客户端只保存一个不透明游标。三件事必须由它保证：

    1. 单调：`version` 只增不减，任何一行发生变化都取一个新号；
    2. 单调的来源可原子化：`UPDATE ... SET version = version + 1 ... RETURNING`
       是一条语句，SQLite 只允许一个写者，因此并发的两次 push 不会拿到同一个号；
    3. 与业务写入同事务：发号与写行必须一起提交，否则客户端可能拉到
       「版本号已前进、行还没写」的空洞。

    时间戳不能替代它：客户端时钟跨设备不可比，服务端时间戳在同一毫秒也会重复。
    """

    __tablename__ = "sync_counter"

    user_id: Mapped[uuid.UUID] = mapped_column(
        SYNC_USER_FK,
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
        SYNC_USER_FK,
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
    都是前端私有结构。**整包存 JSON** 而不是逐列建模：拆成列等于以后前端每改一次
    结构就要一次数据库迁移，而 `app/db.py` 已经明确写着「第二处加列就该换 Alembic」。
    服务端只需要能校验的东西（归属、顺序、大小），不需要理解聊天语义。

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
        SYNC_USER_FK,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    client_rev: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


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


async def _migrate_user_role_column(conn) -> None:  # noqa: ANN001 - AsyncConnection
    """给已存在的 `user` 表补上 `role` 列并回填历史数据（幂等）。

    **为什么需要手写**：`create_all` 只创建缺失的**表**，对已存在的表一个字节都不
    改。而 `bennett.db` 里已经有账号了，不加这一步，升级后每次查询都会撞上
    `no such column: user.role`——表现为「整个站点 500」。

    四步，**顺序不可换**，且每一步都要让后续步骤不再命中它：

    1. 列不存在 → `ALTER TABLE user ADD COLUMN role`（SQLite 允许带 DEFAULT 的 NOT NULL
       加列，已有行自动取默认值 `'user'`）；
    2. **归一化大小写与空白**（`lower(trim(role))`）：手工改库写成 `'ADMIN'` / `' admin '`
       的行先变成 `'admin'`，否则第 4 步会把它们当成非法值丢掉；
    3. **回填**：仍是 `'user'` 且 `is_superuser=1` 的账号是角色列出现之前的历史超管，
       升级为 `superadmin`——否则老管理员会在升级瞬间失去入口，而服务里**没有**任何
       自助恢复路径；
    4. **规范化非法值**：认不出的值降为 `'user'`，**同时清掉 `is_superuser`**。

    第 4 步那个「同时清掉」是幂等的关键：不清的话该行变成 `role='user'` 但
    `is_superuser` 仍是 1，下次启动第 3 步就会把一枚写错的 role 值提成 `superadmin`
    ——正是 HTTP 层刻意禁止授予的档位（`app/api/admin.py` 的 `set_user_role` 只能
    由超管调用且不能改自己）。清理之后结果与启动次数无关，且失败方向是**降权**
    而不是提权。

    归一化口径必须与 `app/roles.py::Role.parse` 一致（`strip().lower()`）：两处不一致
    时，`Role.parse('ADMIN')` 说是管理员、而这里把它降成普通用户，运维手改库的行为
    就成了看运气。
    """
    columns = {row[1] for row in (await conn.execute(text("PRAGMA table_info(user)"))).fetchall()}
    if not columns:
        # 表还不存在（全新库且 create_all 尚未建出）——create_all 会带上 role，无需处理
        return

    if "role" not in columns:
        await conn.execute(text(f"ALTER TABLE user ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT '{ROLE_USER}'"))
        logger.info("已为 user 表添加 role 列（默认 %s）", ROLE_USER)

    # 2. 归一化：只动需要动的行，重复执行时 rowcount 为 0。
    #    SQLite 的 lower() 只处理 ASCII，而 role 的全部取值都是 ASCII，够用。
    normalized_case = await conn.execute(
        text("UPDATE user SET role = lower(trim(role)) WHERE role <> lower(trim(role))")
    )
    if normalized_case.rowcount:
        logger.info("已归一化 %d 个 role 值的大小写/空白", normalized_case.rowcount)

    # 3. 回填：必须在归一化之后，否则 'ADMIN' 会先被第 4 步降成 'user' 再被这里提权。
    backfilled = await conn.execute(
        text("UPDATE user SET role = :admin WHERE is_superuser = 1 AND role = :user").bindparams(
            admin=ROLE_SUPERADMIN, user=ROLE_USER
        )
    )
    if backfilled.rowcount:
        logger.info("已把 %d 个历史超级管理员回填为 role=%s", backfilled.rowcount, ROLE_SUPERADMIN)

    # 4. 规范化非法值。占位符显式展开，不拼字符串（role 值来自本模块常量，非常不可信
    #    输入，但仍不把变量名拼进 SQL）。
    placeholders = ", ".join(f":r{index}" for index in range(len(ALL_ROLES)))
    role_params: dict[str, str] = {f"r{index}": value for index, value in enumerate(ALL_ROLES)}
    invalid = await conn.execute(
        text(f"SELECT DISTINCT role FROM user WHERE role NOT IN ({placeholders})").bindparams(**role_params)
    )
    bad_values = [row[0] for row in invalid.fetchall()]
    if not bad_values:
        return

    # is_superuser 一并清零：否则下次启动会被第 3 步当成历史超管提权（见 docstring）。
    dropped = await conn.execute(
        text(f"UPDATE user SET role = :user, is_superuser = 0 WHERE role NOT IN ({placeholders})").bindparams(
            **role_params, user=ROLE_USER
        )
    )
    logger.warning(
        "已把 %d 个非法 role 值降为 %s 并清除 is_superuser（非法取值: %s）",
        dropped.rowcount,
        ROLE_USER,
        ", ".join(repr(value) for value in bad_values),
    )


async def create_db_and_tables() -> None:
    """启动时建表 + 补齐列（幂等）。

    新增的 `model_provider` / `provider_model` / `conversation` / `message` /
    `sync_counter` 表由 `create_all` 自动创建；已有表（user / access_token）不会被动到。

    **没有引入 Alembic**：迁移需求目前只有「给 user 表加 role 列」这一处，为它引入
    一整套工具链不划算。代价是新增的列改动必须在 `_migrate_user_role_column` 里手写
    并保持幂等。**再出现第二处加列时应该换成 Alembic**，而不是继续往这里堆。
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_user_role_column(conn)

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
