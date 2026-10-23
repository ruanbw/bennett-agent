"""跨设备同步的服务层：服务端权威日志 + 服务端版本号。

## 为什么是这个形状

会话与消息是**追加式、单写者**的数据：一条消息的 id 由创建它的设备生成
（`frontend/src/lib/constants.ts:newId`），之后只有那台设备会改它。所以同步
不需要合并算法，只需要三件事：

1. 服务端给每一次写入发一个**单调版本号**（`sync_counter`），客户端保存不透明游标；
2. 拉取按 `version > since` 增量返回，客户端按 id 幂等应用（重复送达是正常现象）；
3. 删除用**会话级墓碑**传播（`conversation.deleted`），消息级不需要墓碑——应用
   从不删除单条消息，只删整个会话。

这套模式在业界有现成的名字与实现：Microsoft Graph 的 delta query（`deltaLink` +
410 全量重同步 + 必须容忍 replay）、Matrix 的 `/sync`（`since` → `next_batch`）、
Discord 的 Snowflake + 分区。差别只在规模组件，不在协议形状。

## 三条不可让步的规则

- **顺序由服务端决定**。消息的 `seq` 在插入时赋值，之后不再变；客户端的
  `created_at` 只用于展示。客户端时钟跨设备不可比，拿它排序会在换设备后看到乱序。
- **写入与发号同事务**。否则会出现「版本号已前进、行还没写」的空洞，客户端拉到
  空页后把游标推过去，从而永久丢数据。
- **游标只在两路都拉完时前进**。会话与消息是两个数据流，游标取两路的最小停顿
  位置；同一行被重复返回是可以接受（甚至是被期望）的。

## 并发

SQLite 只允许一个写者，`UPDATE ... RETURNING` 是单条语句，因此发号不会撞号。
同一批 id 被并发推送（首次推送与重试同时在飞）会撞主键，此时整批重放一次即可：
upsert 本身幂等。更复杂的并发控制在这个部署形态下没有必要。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal
from weakref import WeakValueDictionary

from sqlalchemy import delete, func, select, update

# PostgreSQL 的 `INSERT ... ON CONFLICT DO UPDATE ... RETURNING`。
# 曾经误用的是 SQLite 方言的同名构造：它在 SQLite 上工作，在 PG 上编译期就抛
# `AttributeError: 'OnConflictDoUpdate' object has no attribute 'constraint_target'`
# ——方言对象不可跨库混用，测试基座换库时这类耦合必须一起拔掉。
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import Conversation, Message, SyncCounter, async_session_maker
from app.schemas.sync import (
    CURSOR_PREFIX,
    ConversationIn,
    ConversationOut,
    MessageIn,
    MessageOut,
    PullResult,
    PushAccepted,
    PushRequest,
    PushResult,
    RejectedRow,
    RejectReason,
)

logger = logging.getLogger(__name__)

#: `WHERE id IN (...)` 的单批大小。SQLite 默认最多 999 个绑定参数，留足余量。
_IN_CHUNK = 400


class SyncError(Exception):
    """同步失败的基类；HTTP 层只负责把它映射成状态码与文案。"""


class InvalidCursor(SyncError):
    """游标格式不认识（客户端 bug 或协议升级）。"""


class StaleCursor(SyncError):
    """游标指向当前数据之外（服务端回滚 / 旧库恢复）。客户端应重新全量同步。"""


class TooManyRows(SyncError):
    """单次 push 行数超限。客户端按 `SYNC_PUSH_MAX_ROWS` 分批。"""


# ---------------------------------------------------------------------
# 游标
# ---------------------------------------------------------------------


def encode_cursor(version: int) -> str:
    """编码成不透明字符串。客户端不许解析，服务端可随时换格式。"""
    return f"{CURSOR_PREFIX}:{version}"


def parse_cursor(raw: str | None) -> int:
    """解析游标；缺省（首次同步）表示从头开始。"""
    if raw is None or not raw.strip():
        return 0
    prefix, separator, value = raw.strip().partition(":")
    if not separator or prefix != CURSOR_PREFIX:
        raise InvalidCursor(f"无法识别的同步游标：{raw!r}")
    try:
        version = int(value)
    except ValueError as exc:
        raise InvalidCursor(f"无法识别的同步游标：{raw!r}") from exc
    if version < 0:
        raise InvalidCursor(f"同步游标不能为负：{raw!r}")
    return version


# ---------------------------------------------------------------------
# 长轮询唤醒
# ---------------------------------------------------------------------

#: 每用户一个唤醒事件。WeakValueDictionary：没有请求在等的时候事件会被回收，
#: 不会因为历史用户数量增长而泄漏内存。
_events: WeakValueDictionary[uuid.UUID, asyncio.Event] = WeakValueDictionary()


def _event_for(user_id: uuid.UUID) -> asyncio.Event:
    event = _events.get(user_id)
    if event is None:
        event = asyncio.Event()
        _events[user_id] = event
    return event


def notify_change(user_id: uuid.UUID) -> None:
    """**提交之后**才调用：提交前唤醒会让等待方读到旧版本号，然后白白再等一轮。"""
    event = _events.get(user_id)
    if event is not None:
        event.set()


def reset_notifier() -> None:
    """清空唤醒表（测试与进程关闭用）。asyncio.Event 会绑定创建时的 event loop，
    跨 loop 复用会抛「is bound to a different event loop」。"""
    _events.clear()


async def wait_for_change(user_id: uuid.UUID, since: int, timeout: float) -> None:
    """挂住 `timeout` 秒，直到该用户有新的同步版本或超时。

    先 `clear()` 再查版本号是关键顺序：如果反了，一次发生在「查完」与「开始等」
    之间的写入会把事件设成 set，而我们在它之后才 clear，于是丢掉这次唤醒。
    """
    if timeout <= 0:
        return
    event = _event_for(user_id)
    event.clear()
    async with async_session_maker() as session:
        if await _head_version(session, user_id) > since:
            return
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(event.wait(), timeout=timeout)


# ---------------------------------------------------------------------
# 版本号
# ---------------------------------------------------------------------


async def _next_version(session: AsyncSession, user_id: uuid.UUID) -> int:
    """原子发号：一条语句完成「没有就建、有就 +1 并返回」。"""
    statement = (
        pg_insert(SyncCounter)
        .values(user_id=user_id, version=1)
        .on_conflict_do_update(
            index_elements=[SyncCounter.user_id],
            set_={SyncCounter.version: SyncCounter.version + 1},
        )
        .returning(SyncCounter.version)
    )
    return int((await session.execute(statement)).scalar_one())


async def _head_version(session: AsyncSession, user_id: uuid.UUID) -> int:
    value = await session.scalar(select(SyncCounter.version).where(SyncCounter.user_id == user_id))
    return int(value or 0)


async def head_version(user_id: uuid.UUID) -> int:
    async with async_session_maker() as session:
        return await _head_version(session, user_id)


# ---------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------


def _chunks(values: Sequence[str], size: int = _IN_CHUNK) -> Iterable[Sequence[str]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _dedupe_conversations(rows: Sequence[ConversationIn]) -> list[ConversationIn]:
    """同一批里重复的 id 取 `client_rev` 最大的那条。

    重复出现在重试拼接的批次里（客户端把「本批」和「上次失败的本批」并起来发了），
    取最大的修订号才是它真正想表达的内容。
    """
    latest: dict[str, ConversationIn] = {}
    for row in rows:
        current = latest.get(row.id)
        if current is None or row.client_rev >= current.client_rev:
            latest[row.id] = row
    return list(latest.values())


def _dedupe_messages(rows: Sequence[MessageIn]) -> list[MessageIn]:
    latest: dict[str, MessageIn] = {}
    for row in rows:
        current = latest.get(row.id)
        if current is None or row.client_rev >= current.client_rev:
            latest[row.id] = row
    return list(latest.values())


def _payload_size(payload: dict[str, object]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _reject(kind: Literal["conversation", "message"], row_id: str, reason: RejectReason) -> RejectedRow:
    return RejectedRow(kind=kind, id=row_id, reason=reason)


@dataclass(frozen=True)
class _ConversationMeta:
    """现有会话行的元数据（不读大字段）。可变字段用于判断「同修订号但内容不同」。"""

    user_id: uuid.UUID
    deleted: bool
    client_rev: int
    title: str
    model: str | None
    reasoning_effort: str | None
    is_pinned: bool


@dataclass(frozen=True)
class _MessageMeta:
    user_id: uuid.UUID
    conversation_id: str
    role: str
    client_rev: int
    seq: int


async def _existing_conversations(session: AsyncSession, ids: Sequence[str]) -> dict[str, _ConversationMeta]:
    """按 id 取现有会话的**元数据**（不读大字段）。

    刻意不加 `user_id` 过滤：要能发现「这个 id 属于别人」，才能把它按 `not_owned`
    拒绝，而不是让 upsert 静默失败或误写别人的行。只读 id/属主/状态，不返回内容。
    """
    found: dict[str, _ConversationMeta] = {}
    for chunk in _chunks(sorted(set(ids))):
        rows = await session.execute(
            select(
                Conversation.id,
                Conversation.user_id,
                Conversation.deleted,
                Conversation.client_rev,
                Conversation.title,
                Conversation.model,
                Conversation.reasoning_effort,
                Conversation.is_pinned,
            ).where(Conversation.id.in_(chunk))
        )
        for row_id, owner, deleted, client_rev, title, model, effort, pinned in rows.all():
            found[row_id] = _ConversationMeta(
                user_id=owner,
                deleted=deleted,
                client_rev=client_rev,
                title=title,
                model=model,
                reasoning_effort=effort,
                is_pinned=pinned,
            )
    return found


async def _existing_messages(session: AsyncSession, ids: Sequence[str]) -> dict[str, _MessageMeta]:
    found: dict[str, _MessageMeta] = {}
    for chunk in _chunks(sorted(set(ids))):
        rows = await session.execute(
            select(
                Message.id,
                Message.user_id,
                Message.conversation_id,
                Message.role,
                Message.client_rev,
                Message.seq,
            ).where(Message.id.in_(chunk))
        )
        for row_id, owner, conversation_id, role, client_rev, seq in rows.all():
            found[row_id] = _MessageMeta(
                user_id=owner,
                conversation_id=conversation_id,
                role=role,
                client_rev=client_rev,
                seq=seq,
            )
    return found


async def _existing_message_payloads(session: AsyncSession, ids: Sequence[str]) -> dict[str, dict]:
    """只给「修订号相同」的行读 payload（重放批次里通常为零或全批）。

    这是判断「同修订号到底是纯重放，还是客户端漏了 +1」的唯一办法。payload 是整包
    JSON，读它不便宜，所以只在必要时调用。
    """
    found: dict[str, dict] = {}
    for chunk in _chunks(sorted(set(ids))):
        rows = await session.execute(select(Message.id, Message.payload).where(Message.id.in_(chunk)))
        for row_id, payload in rows.all():
            found[row_id] = payload
    return found


def _conversation_signature(value: ConversationIn | _ConversationMeta) -> tuple:
    """会话里「可变内容」的指纹，用来区分纯重放与内容变更。

    `deleted` 必须在里面：漏掉它的话，一个「修订号没变但要把会话删掉」的推送会被判成
    纯重放而**静默丢弃**——墓碑不写、消息不删、版本不前进，删除永久丢失。
    """
    return (
        value.title,
        value.model,
        value.reasoning_effort,
        bool(value.is_pinned),
        bool(value.deleted),
    )


async def _next_seq(session: AsyncSession, conversation_id: str) -> int:
    """会话内顺序发号。与消息插入同事务：回滚时序号一起回滚。"""
    statement = (
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(seq_counter=Conversation.seq_counter + 1)
        .returning(Conversation.seq_counter)
    )
    value = (await session.execute(statement)).scalar_one_or_none()
    if value is None:
        # 调用方已经校验过会话存在；走到这里说明数据在事务里被并发删掉了。
        raise SyncError(f"会话不存在，无法分配顺序号：{conversation_id}")
    return int(value)


# ---------------------------------------------------------------------
# push
# ---------------------------------------------------------------------

#: 单进程内的 push 串行锁。
#:
#: 配额（会话数 / 单会话消息数）是「先 COUNT 再 INSERT」，两个并发请求各自在
#: 自己的事务里读到同一个旧计数，就会同时突破上限（SQLite 单写者只保证写入
#: 串行，不保证「读到的计数在写时仍然有效」）。把整个 push 串起来之后，计数与
#: 插入之间不再有第二个写者。多 worker 部署下这把锁只覆盖单进程，但 SQLite
#: 文件本身就是单机部署形态（见 app/db.py 的说明）；需要多实例时应换库并用
#: 数据库约束/行锁实现配额。
_push_lock: asyncio.Lock | None = None
_push_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_push_lock() -> asyncio.Lock:
    """取当前事件循环的 push 锁；loop 变了就重建（测试每个用例一个新 loop）。"""
    global _push_lock, _push_lock_loop
    loop = asyncio.get_running_loop()
    if _push_lock is None or _push_lock_loop is not loop:
        _push_lock = asyncio.Lock()
        _push_lock_loop = loop
    return _push_lock


def reset_push_lock() -> None:
    """清空 push 锁（测试用；与 `reset_notifier` 同口径）。"""
    global _push_lock, _push_lock_loop
    _push_lock = None
    _push_lock_loop = None


async def push(user_id: uuid.UUID, request: PushRequest) -> PushResult:
    """批量写入（幂等 upsert），逐行拒绝坏数据。"""
    total = len(request.conversations) + len(request.messages)
    if total > settings.sync_push_max_rows:
        raise TooManyRows(f"单次最多推送 {settings.sync_push_max_rows} 行，本次 {total} 行")

    async with _get_push_lock():
        try:
            return await _push_once(user_id, request)
        except IntegrityError:
            # 同一批 id 被并发推送（首次推送与重试同时在飞）。upsert 本身幂等，
            # 整批重放一次就落到已有行上了；第二次还撞就说明是真故障，往上抛。
            logger.warning("并发推送撞主键，整批重放一次 (user=%s rows=%d)", user_id, total)
            return await _push_once(user_id, request)


async def _push_once(user_id: uuid.UUID, request: PushRequest) -> PushResult:
    rejected: list[RejectedRow] = []
    accepted_conversations = 0
    accepted_messages = 0
    #: 是否真的写进了行。纯重放（内容完全相同）不写库也不推进版本，
    #: 因此不该唤醒该用户其它设备的长轮询——那只会让它们空拉一次再挂回去。
    wrote_rows = False

    async with async_session_maker() as session, session.begin():
        conversation_rows = _dedupe_conversations(request.conversations)
        message_rows = _dedupe_messages(request.messages)

        existing_conversations = await _existing_conversations(
            session,
            [row.id for row in conversation_rows] + [row.conversation_id for row in message_rows],
        )

        # --- 会话 -------------------------------------------------
        writable: set[str] = set()
        #: 已删的会话（服务端墓碑 + 本批刚删的）。消息写进来时优先报这个原因，
        #: 比 conversation_missing 更准确——客户端据此丢掉这些行而不是重试。
        deleted_ids: set[str] = {
            row_id for row_id, meta in existing_conversations.items() if meta.user_id == user_id and meta.deleted
        }
        # 只统计**未删除**会话：墓碑是删除信号，不该永久占用「会话数」配额。
        # 新墓碑不再落库（见下面的分支），因此墓碑行数只可能来自「删除已有会话」，
        # 上限天然被 live 配额约束，不需要单独配额（L-11/H-02）。
        live_count = int(
            await session.scalar(
                select(func.count())
                .select_from(Conversation)
                .where(Conversation.user_id == user_id, Conversation.deleted.is_(False))
            )
            or 0
        )
        quota_left = settings.sync_max_conversations_per_user - live_count

        for row in sorted(conversation_rows, key=lambda item: item.id):
            current = existing_conversations.get(row.id)
            if current is not None:
                if current.user_id != user_id:
                    rejected.append(_reject("conversation", row.id, "not_owned"))
                    continue
                # 墓碑不能被「未删除」的行复活：删除是单向的。
                if current.deleted and not row.deleted:
                    rejected.append(_reject("conversation", row.id, "conversation_deleted"))
                    continue
                if current.client_rev > row.client_rev:
                    rejected.append(_reject("conversation", row.id, "stale"))
                    continue
                if current.client_rev == row.client_rev:
                    if _conversation_signature(current) == _conversation_signature(row):
                        # 完全相同的重放：不写库、不推进版本。否则每次重试都会白白唤醒
                        # 所有设备的 pull，并把同一行再推一遍。
                        accepted_conversations += 1
                        if not row.deleted:
                            writable.add(row.id)
                        continue
                    # 修订号没变但内容变了：客户端漏了 +1（例如直接改了库）。
                    # 静默丢弃会让两端永久不一致，所以按内容应用并留下日志。
                    logger.warning(
                        "同步行修订号未变但内容不同，按内容应用 (conversation=%s rev=%d)",
                        row.id,
                        row.client_rev,
                    )
            elif not row.deleted:
                if quota_left <= 0:
                    rejected.append(_reject("conversation", row.id, "quota_exceeded"))
                    continue
                quota_left -= 1
            else:
                # 新墓碑（服务端从来没有这条会话）：**不写库**，记账为已接受。
                #
                # 两个理由：
                # 1. 墓碑的作用是让其他设备删掉它们已经同步过的行；服务端没有这条行时，
                #    说明没有设备同步过它，墓碑没有传播对象，写下来只是永久垃圾；
                # 2. 允许为任意新 id 写墓碑，等于让任何用户用自选 id **预占**别人未来的
                #    会话 id（一旦被占，对方后续同 id 推送永远 not_owned），也能靠
                #    deleted=true 无限刷行（L-11/H-02）。
                #
                # 客户端拿到「已接受」不会重试；之后它若真把这条会话推上来，仍按新建处理。
                # 同批里引用它的消息仍按「已删除」拒绝（与有墓碑时语义一致）。
                deleted_ids.add(row.id)
                accepted_conversations += 1
                continue

            version = await _next_version(session, user_id)
            statement = (
                pg_insert(Conversation)
                .values(
                    id=row.id,
                    user_id=user_id,
                    title=row.title,
                    model=row.model,
                    reasoning_effort=row.reasoning_effort,
                    is_pinned=row.is_pinned,
                    deleted=row.deleted,
                    client_rev=row.client_rev,
                    version=version,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                )
                .on_conflict_do_update(
                    index_elements=[Conversation.id],
                    set_={
                        "title": row.title,
                        "model": row.model,
                        "reasoning_effort": row.reasoning_effort,
                        "is_pinned": row.is_pinned,
                        "deleted": row.deleted,
                        "client_rev": row.client_rev,
                        "version": version,
                        "updated_at": row.updated_at,
                    },
                    where=(Conversation.user_id == user_id) & (Conversation.client_rev <= row.client_rev),
                )
                .returning(Conversation.id)
            )
            if (await session.execute(statement)).scalar_one_or_none() is None:
                # 前置校验已挡住所有已知情况；留着这条兜住未来的新分支，
                # 宁可少写一行并明确拒绝，也不要静默变成「推送成功」。
                rejected.append(_reject("conversation", row.id, "not_owned"))
                continue

            accepted_conversations += 1
            wrote_rows = True
            if row.deleted:
                deleted_ids.add(row.id)
                # 墓碑留下，消息清掉：墓碑是删除信号，消息没有保留价值。
                await session.execute(delete(Message).where(Message.conversation_id == row.id))
            else:
                writable.add(row.id)

        # 已存在且属于自己、且没删的会话同样可以写消息
        for row_id, meta in existing_conversations.items():
            if meta.user_id == user_id and not meta.deleted and row_id not in deleted_ids:
                writable.add(row_id)

        # --- 消息 -------------------------------------------------
        existing_messages = await _existing_messages(session, [row.id for row in message_rows])
        message_counts: dict[str, int] = {}
        if writable:
            counts = await session.execute(
                select(Message.conversation_id, func.count())
                .where(Message.conversation_id.in_(sorted(writable)))
                .group_by(Message.conversation_id)
            )
            message_counts = {conversation_id: int(count) for conversation_id, count in counts.all()}

        # 修订号相同的行才需要读 payload 比对；纯重放批次里这一步是 O(1) 次往返。
        equal_rev_ids = [
            row.id
            for row in message_rows
            if (meta := existing_messages.get(row.id)) is not None
            and meta.user_id == user_id
            and meta.client_rev == row.client_rev
        ]
        unchanged_payloads = await _existing_message_payloads(session, equal_rev_ids) if equal_rev_ids else {}

        ordered = sorted(message_rows, key=lambda item: (item.conversation_id, item.created_at, item.id))
        for row in ordered:
            current = existing_messages.get(row.id)
            if current is not None and current.user_id != user_id:
                # 不区分「存在但不是你的」与「会话不存在」：否则可以拿消息 id 探测
                # 别人的行是否存在（L-11）。消息 id 是 uuid，正常客户端不会碰到；
                # 真碰到时按不可写入处理即可。
                rejected.append(_reject("message", row.id, "conversation_missing"))
                continue
            if row.conversation_id in deleted_ids:
                rejected.append(_reject("message", row.id, "conversation_deleted"))
                continue
            if row.conversation_id not in writable:
                rejected.append(_reject("message", row.id, "conversation_missing"))
                continue
            if current is not None and current.client_rev > row.client_rev:
                rejected.append(_reject("message", row.id, "stale"))
                continue
            if current is not None and current.client_rev == row.client_rev:
                if current.role == row.role and unchanged_payloads.get(row.id) == row.payload:
                    # 纯重放：只记账，不落库、不推进版本。
                    accepted_messages += 1
                    continue
                # 修订号没变但内容变了：见会话分支的同一条理由。
                logger.warning("同步行修订号未变但内容不同，按内容应用 (message=%s rev=%d)", row.id, row.client_rev)
            if _payload_size(row.payload) > settings.sync_payload_max_bytes:
                rejected.append(_reject("message", row.id, "too_large"))
                continue

            if current is None:
                used = message_counts.get(row.conversation_id, 0)
                if used >= settings.sync_max_messages_per_conversation:
                    rejected.append(_reject("message", row.id, "quota_exceeded"))
                    continue
                message_counts[row.conversation_id] = used + 1
                # seq 只在插入时赋值：后面内容再变（流式助手消息）也不许动它，
                # 否则消息会在列表里跳位。
                seq = await _next_seq(session, row.conversation_id)
            else:
                seq = current.seq

            version = await _next_version(session, user_id)
            statement = (
                pg_insert(Message)
                .values(
                    id=row.id,
                    conversation_id=row.conversation_id,
                    user_id=user_id,
                    role=row.role,
                    payload=row.payload,
                    client_rev=row.client_rev,
                    seq=seq,
                    version=version,
                    created_at=row.created_at,
                )
                .on_conflict_do_update(
                    index_elements=[Message.id],
                    set_={
                        "payload": row.payload,
                        "role": row.role,
                        "client_rev": row.client_rev,
                        "version": version,
                    },
                    where=(Message.user_id == user_id) & (Message.client_rev <= row.client_rev),
                )
                .returning(Message.id)
            )
            if (await session.execute(statement)).scalar_one_or_none() is None:
                rejected.append(_reject("message", row.id, "not_owned"))
                continue
            accepted_messages += 1
            wrote_rows = True

        version_after = await _head_version(session, user_id)

    if wrote_rows:
        # 提交之后再唤醒：提交前唤醒会让等待方读到旧版本，然后白等一轮。
        notify_change(user_id)

    logger.info(
        "同步推送 user=%s conversations=%d messages=%d rejected=%d version=%d",
        user_id,
        accepted_conversations,
        accepted_messages,
        len(rejected),
        version_after,
    )
    return PushResult(
        version=version_after,
        accepted=PushAccepted(conversations=accepted_conversations, messages=accepted_messages),
        rejected=rejected,
    )


# ---------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------


async def pull(user_id: uuid.UUID, since: int, limit: int | None = None) -> PullResult:
    """增量拉取：返回 `version > since` 的会话与消息。"""
    max_rows = settings.sync_pull_max_rows
    if limit is not None and limit > 0:
        max_rows = min(limit, max_rows)

    async with async_session_maker() as session:
        head = await _head_version(session, user_id)
        if since > head:
            raise StaleCursor(f"游标 {since} 超出服务端当前版本 {head}")

        conversation_rows = list(
            (
                await session.execute(
                    select(Conversation)
                    .where(Conversation.user_id == user_id, Conversation.version > since)
                    .order_by(Conversation.version)
                    .limit(max_rows + 1)
                )
            )
            .scalars()
            .all()
        )
        message_rows = list(
            (
                await session.execute(
                    select(Message)
                    .where(Message.user_id == user_id, Message.version > since)
                    .order_by(Message.version)
                    .limit(max_rows + 1)
                )
            )
            .scalars()
            .all()
        )

        conversations_more = len(conversation_rows) > max_rows
        messages_more = len(message_rows) > max_rows
        if conversations_more:
            conversation_rows = conversation_rows[:max_rows]
        if messages_more:
            message_rows = message_rows[:max_rows]

        # 行数上限挡不住大响应：500 行 × 256 KB 最坏是 128 MB。再按累计字节截一次。
        # 至少保留一行（否则超大单行会让游标原地打转），剩下的下一次 pull 继续。
        kept: list[Message] = []
        used_bytes = 0
        for row in message_rows:
            size = _payload_size(row.payload)
            if kept and used_bytes + size > settings.sync_pull_max_bytes:
                messages_more = True
                break
            kept.append(row)
            used_bytes += size
        if len(kept) != len(message_rows):
            messages_more = True
        message_rows = kept

        # 游标只能在「两路都拉完」的位置前进。若只截断了一路，就以那一路的最后一行
        # 为准；两路都截断时取较小的那个。被多返回的行客户端会重复收到一次，无害。
        stops = []
        if conversations_more:
            stops.append(conversation_rows[-1].version)
        if messages_more:
            stops.append(message_rows[-1].version)
        cursor_version = min(stops) if stops else head

        return PullResult(
            cursor=encode_cursor(cursor_version),
            has_more=conversations_more or messages_more,
            conversations=[ConversationOut.model_validate(row) for row in conversation_rows],
            messages=[MessageOut.model_validate(row) for row in message_rows],
        )
