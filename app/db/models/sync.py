"""跨设备同步表：会话与消息的服务端镜像，`version` 是拉取游标。"""

import uuid

from fastapi_users_db_sqlalchemy.generics import GUID
from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


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
