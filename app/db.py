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

import base64
import hashlib
import logging
import os
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
    JSON,
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
    event,
    text,
)
from sqlalchemy import Uuid as SQLUuid
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.config import settings
from app.config.secret_box import decrypt_secret, encrypt_secret
from app.roles import ALL_ROLES, ROLE_SUPERADMIN, ROLE_USER, Role

logger = logging.getLogger(__name__)


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
    """写入摘要、按摘要查询；同时兼容历史明文行。

    `get_by_token` 先按摘要查（新行），查不到再按原文查（旧行）——旧会话在过期前
    仍可用，不会因为升级把所有人踢下线。
    """

    async def get_by_token(self, token: str, max_age=None):  # noqa: ANN001, ANN201
        row = await super().get_by_token(hash_access_token(token), max_age)
        if row is not None:
            return row
        return await super().get_by_token(token, max_age)


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


class ModelPrice(Base):
    """模型的计价信息（每百万 token 的输入/输出价格）。

    ## 为什么是独立表而不是给 `provider_model` 加两列

    两个理由，第二个更重要：

    1. `create_all` **不会**给已存在的表加列，加列必须手写幂等迁移。`app/db.py` 里
       那条注释明确写着「再出现第二处加列时应该换成 Alembic」——而独立新表由
       `create_all` 直接建出来，不需要引入整套迁移工具链。
    2. 价格是**独立生命周期**的数据：它可以缺（未知模型）、会变（上游调价）、也可能
       由运维批量导入。混进模型元数据表会让「同步上游模型列表」这条路径有覆盖它的
       风险，而价格被同步动作悄悄清掉是最难发现的一类账目错误。

    ## 单位与精度

    `*_per_mtok` 是「每百万 token 的价格」，单位是**计价货币**（`currency`，默认 USD）。
    用百万为单位而不是每 token：后者是 1e-6 量级的小数，浮点误差与展示都难处理。

    `Decimal` 而不是 `Float`：钱不能有二进制浮点误差。SQLite 里存成 NUMERIC，
    SQLAlchemy 会经 `Decimal` 往返——累加几百次仍然精确。

    ## 为什么允许为空

    绝大多数自建网关与本地模型（vLLM、LiteLLM 本地代理）没有公开计价，或者部署方
    根本不在乎这个数。缺价格时前端显示「未配置价格」并**不显示金额**——显示 0 会被
    读成「免费」，那是一个明确的错误结论。
    """

    __tablename__ = "model_price"
    __table_args__ = (UniqueConstraint("model_id", name="uq_model_price_model"),)

    id: Mapped[uuid.UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
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

    id: Mapped[uuid.UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: 指向 user 的外键必须是 `GUID`，不能用 `SQLUuid`：两者在 SQLite 下存成
    #: CHAR(36) 带横线 与 CHAR(32) 不带横线，字面量不同，外键约束会**直接拒绝**
    #: ——症状是 500 `FOREIGN KEY constraint failed`。详见上面的 `SYNC_USER_FK`。
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

    向量存成 float32 的二进制 BLOB（`app/services/rag/store.py:pack_vector`）而不是
    JSON：1024 维的 JSON 文本约 12KB，二进制是 4KB，且读出来不用 `json.loads`。
    多文档检索时这个差别就是几十毫秒。

    `user_id` 与 `document_id` 冗余一份：检索按用户过滤时不必 join 文档表，
    索引能直接命中（与 `message.user_id` 同一条取舍）。
    """

    __tablename__ = "knowledge_chunk"
    __table_args__ = (
        Index("ix_knowledge_chunk_user", "user_id"),
        Index("ix_knowledge_chunk_document", "document_id", "chunk_index"),
    )

    id: Mapped[uuid.UUID] = mapped_column(SQLUuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        SQLUuid(as_uuid=True),
        ForeignKey("knowledge_document.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: 同 `KnowledgeDocument.user_id`：指向 user 必须用 `GUID`。
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


def secure_sqlite_permissions(path: str | Path) -> bool:
    """把 SQLite 文件权限收紧到 0600（仅属主可读写）。

    `bennett.db` 里有密码哈希、会话摘要与（现在加密的）API Key；默认 umask 常是
    022，同主机其他用户可读。数据库文件不需要组/其他用户访问。失败只告警——
    文件系统不支持 chmod（某些挂载）时不该让服务起不来。
    """
    try:
        os.chmod(path, 0o600)
        return True
    except OSError:
        logger.warning("收紧 SQLite 文件权限失败（不影响启动）: %s", path, exc_info=True)
        return False


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


def apply_sqlite_pragmas(dbapi_connection) -> None:  # noqa: ANN001 - DBAPI connection
    """给一个 SQLite 连接设置本项目需要的 PRAGMA（幂等）。

    SQLite 的 PRAGMA 是**每连接**生效，不是数据库级的持久设置。

        - `foreign_keys=ON`：SQLite 默认关闭外键约束，不开则级联删除失效。
        - `journal_mode=WAL`：读写不互相阻塞，避免流式对话期间的写锁卡住读请求。
          **注意它不是每个文件都能生效**：网络文件系统/某些容器卷会静默退回
          `delete` 模式（见 `check_journal_mode`）。
        - `synchronous=NORMAL`：WAL 下这是官方推荐的搭配。默认的 `FULL` 每次提交都
          fsync，而 WAL + NORMAL 只在检查点 fsync——掉电最多丢最后几个事务（对聊天
          记录可接受），换来的是流式落库不再被磁盘拖慢。`OFF` 绝对不能用：那是真会坏库。
        - `busy_timeout=5000`：并发写入时排队等待而不是立刻抛 `database is locked`。

    独立成模块级函数（而不是只做事件回调）：测试可以直接拿一条裸 `sqlite3` 连接调用它，
    不用去动全局 engine。
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


if engine.dialect.name == "sqlite":

    @event.listens_for(engine.sync_engine, "connect")
    def _apply_sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
        apply_sqlite_pragmas(dbapi_connection)


async def check_journal_mode(conn) -> str:  # noqa: ANN001 - AsyncConnection
    """返回数据库当前实际使用的日志模式（小写）。

    为什么要在启动时查一次：`PRAGMA journal_mode=WAL` **可能静默失败**——SQLite 在网络
    文件系统（NFS/SMB）、某些只读挂载或容器卷上会返回 `delete`，而不是报错。此时一切
    照常工作，只是并发读写开始互相阻塞，表现为「流式回答进行中查列表会卡住」，而日志里
    没有任何线索。README 已经写明「SQLite 文件不要放在 NFS 上」，这条检查就是把它变成
    可观测的。

    查询失败不抛：这是诊断信息，不该让服务起不来。
    """
    try:
        row = (await conn.exec_driver_sql("PRAGMA journal_mode")).fetchone()
    except Exception:  # noqa: BLE001 - 诊断失败不影响启动
        logger.warning("读取 SQLite journal_mode 失败，跳过 WAL 检查", exc_info=True)
        return ""
    return str(row[0]).lower() if row else ""


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


def _model_columns() -> dict[str, set[str]]:
    """ORM 声明的 `{表名: 列名集合}`（不读库，纯元数据）。"""
    return {table.name: {column.name for column in table.columns} for table in Base.metadata.sorted_tables}


def diff_schema(actual: dict[str, set[str]]) -> dict[str, set[str]]:
    """比对「代码里的表结构」与「库里的表结构」，返回**库里缺的列**。

    `create_all` 只建缺失的表，对已存在的表一个字节都不改：新增一列而忘了写迁移时，
    服务能正常启动，直到某个查询撞上 `no such column` 才 500——而那时用户已经在用了。
    这个比对在启动时就把缺口指出来（表名 + 列名 + 该怎么补），比运行期报错可查得多。

    只报「模型有、库里没有」的列。库里多出来的列不报：那可能是旧版本遗留，删它才危险。
    表整个缺失也不报（`create_all` 会建出来）。
    """
    expected = _model_columns()
    missing: dict[str, set[str]] = {}
    for table, columns in expected.items():
        present = actual.get(table)
        if present is None:
            continue
        gap = columns - present
        if gap:
            missing[table] = gap
    return missing


def diff_column_constraints(actual: dict[str, dict[str, bool]]) -> dict[str, set[str]]:
    """比对「列是否允许 NULL」，返回**库里比代码更严格**的列。

    `actual` 是 `{表名: {列名: 是否 NOT NULL}}`（来自 `PRAGMA table_info`）。

    只报这一个方向：**模型允许 NULL、库里是 NOT NULL**。此时应用会按模型写 NULL，然后在
    运行期撞 `IntegrityError: NOT NULL constraint failed`——而这条路径往往只在某个可选字段
    真的有值时才走到（比如 `null` 的 error 字段），现场极难复现。

    反方向（模型 NOT NULL、库里可空）不报：约束交给应用层，历史库里有旧行本来就是 NULL，
    报出来只会制造噪音。
    """
    stricter: dict[str, set[str]] = {}
    for table in Base.metadata.sorted_tables:
        columns = actual.get(table.name)
        if not columns:
            continue
        bad = {column.name for column in table.columns if column.nullable and columns.get(column.name) is True}
        if bad:
            stricter[table.name] = bad
    return stricter


async def _warn_on_missing_columns(conn) -> None:  # noqa: ANN001 - AsyncConnection
    """启动时检查「加了字段却没写迁移」的漂移，并把缺口写进日志。

    两类漂移：**库里缺列**（查询会 `no such column`）与**库里比代码更严格**（写 NULL 会
    `IntegrityError`）。两者都只在运行期某条冷路径上炸，启动日志是唯一能提前发现的地方。
    """
    actual: dict[str, set[str]] = {}
    not_null: dict[str, dict[str, bool]] = {}
    for table in Base.metadata.sorted_tables:
        rows = (await conn.execute(text(f"PRAGMA table_info({table.name})"))).fetchall()
        if not rows:
            continue
        # PRAGMA table_info 列序：cid, name, type, notnull, dflt_value, pk
        actual[table.name] = {row[1] for row in rows}
        not_null[table.name] = {str(row[1]): bool(row[3]) for row in rows}

    missing = diff_schema(actual)
    if missing:
        detail = "；".join(f"{table} 缺 {', '.join(sorted(columns))}" for table, columns in missing.items())
        logger.warning(
            "数据库结构与代码不一致（%s）。create_all 不会给已存在的表加列，"
            "需要在 app/db.py 里补一段幂等的迁移，否则相关查询会报 no such column。",
            detail,
        )

    stricter = diff_column_constraints(not_null)
    if stricter:
        detail = "；".join(f"{table}.{', '.join(sorted(columns))}" for table, columns in stricter.items())
        logger.warning(
            "数据库里有列比代码更严格（%s 在库里是 NOT NULL，而代码允许 NULL）。"
            "写入 NULL 时会在运行期报 IntegrityError，需要在迁移里重建该表或补默认值。",
            detail,
        )


async def create_db_and_tables() -> None:
    """启动时建表 + 补齐列（幂等）。

    新增的 `model_provider` / `provider_model` / `conversation` / `message` /
    `sync_counter` 表由 `create_all` 自动创建；已有表（user / access_token）不会被动到。

    **没有引入 Alembic**：迁移需求目前只有「给 user 表加 role 列」这一处，为它引入
    一整套工具链不划算。代价是新增的列改动必须在 `_migrate_user_role_column` 里手写
    并保持幂等。**再出现第二处加列时应该换成 Alembic**，而不是继续往这里堆。

    为降低「忘了写迁移」的代价，建表之后会做一次结构比对（`diff_schema`）：缺列时在
    启动日志里点名是哪张表的哪一列，而不是等某个查询在运行期报 `no such column`。
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_user_role_column(conn)
        await _warn_on_missing_columns(conn)
        mode = await check_journal_mode(conn)
        if mode and mode != "wal":
            logger.warning(
                "SQLite 未启用 WAL（当前 journal_mode=%s）。常见原因是数据库文件放在网络文件系统"
                "（NFS/SMB）或不受支持的容器卷上：读写会互相阻塞，流式对话期间查列表可能卡住。"
                "请把数据库放到本地磁盘。",
                mode,
            )

    if engine.dialect.name == "sqlite":
        database = engine.url.database
        if database and database != ":memory:":
            secure_sqlite_permissions(database)

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
    yield HashedAccessTokenDatabase(session, AccessToken)
