"""跨设备同步的行为锁（`app/services/sync_service.py`）。

这一层的错误方式是**静默丢数据**：客户端以为推上去了、或者以为游标已经追平，
于是某条消息在两台设备上永远对不上。所以每个用例都钉住一条不变量：

1. 幂等 —— 同一批重复推送不产生新行、不推进版本；
2. 单调 —— 同一行的内容更新只前进 `version`，`seq` 不变（否则消息会跳位）；
3. 收敛 —— 游标拉完两路后，另一台设备看到的就是全量；
4. 隔离 —— 换一个 `user_id` 什么都读不到、也写不进别人的会话；
5. 删除单向 —— 墓碑不能让「离线设备重新推送」复活；
6. 有界 —— 行数/大小/配额超限时**逐行拒绝**，同批其余行照常成功。

数据库一律用每个用例自己的临时 SQLite 文件：共用库会让「第几个版本号」随用例顺序
漂移，而版本号正是这里最需要精确断言的东西。
"""

import asyncio
import time
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db import Base, Conversation, Message, User
from app.schemas.sync import ConversationIn, MessageIn, PushRequest
from app.services import sync_service


class Env:
    """一个独立库 + 两个用户。"""

    def __init__(self, maker, alice: uuid.UUID, bob: uuid.UUID) -> None:
        self.maker = maker
        self.alice = alice
        self.bob = bob


@pytest.fixture
async def env(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sync.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, expire_on_commit=False)
    alice, bob = uuid.uuid4(), uuid.uuid4()
    async with maker() as session:
        for user_id, email in ((alice, "alice@example.com"), (bob, "bob@example.com")):
            session.add(
                User(
                    id=user_id,
                    email=email,
                    hashed_password="x",
                    is_active=True,
                    is_superuser=False,
                    is_verified=True,
                )
            )
        await session.commit()

    monkeypatch.setattr(sync_service, "async_session_maker", maker)
    sync_service.reset_notifier()
    try:
        yield Env(maker, alice, bob)
    finally:
        sync_service.reset_notifier()
        await engine.dispose()


def conversation(session_id: str, **overrides) -> ConversationIn:
    now = int(time.time() * 1000)
    payload = {
        "id": session_id,
        "title": "标题",
        "model": "auto/x",
        "reasoning_effort": "medium",
        "is_pinned": False,
        "deleted": False,
        "client_rev": 0,
        "created_at": now,
        "updated_at": now,
        **overrides,
    }
    return ConversationIn(**payload)


def message(message_id: str, session_id: str, **overrides) -> MessageIn:
    payload = {
        "id": message_id,
        "conversation_id": session_id,
        "role": "user",
        "payload": {"content": "你好"},
        "client_rev": 0,
        "created_at": int(time.time() * 1000),
        **overrides,
    }
    return MessageIn(**payload)


async def drain(user_id: uuid.UUID, cursor: str | None = None) -> tuple[list[str], list[str], str]:
    """一直拉到没有更多，返回（会话 id、消息 id、最终游标）。重复返回的行按集合去重。"""
    conversations: set[str] = set()
    messages: set[str] = set()
    while True:
        page = await sync_service.pull(user_id, sync_service.parse_cursor(cursor))
        conversations.update(row.id for row in page.conversations)
        messages.update(row.id for row in page.messages)
        cursor = page.cursor
        if not page.has_more:
            return sorted(conversations), sorted(messages), cursor


# ---------------------------------------------------------------------
# 基本往返
# ---------------------------------------------------------------------


async def test_push_then_pull_returns_rows_with_server_assigned_seq(env):
    result = await sync_service.push(
        env.alice,
        PushRequest(
            conversations=[conversation("sess_a")],
            messages=[message("msg_1", "sess_a"), message("msg_2", "sess_a")],
        ),
    )

    assert result.accepted.conversations == 1
    assert result.accepted.messages == 2
    assert result.rejected == []
    assert result.version > 0

    page = await sync_service.pull(env.alice, 0)
    assert [row.id for row in page.conversations] == ["sess_a"]
    assert [row.id for row in page.messages] == ["msg_1", "msg_2"]
    # 顺序由服务端决定，不是客户端时间戳
    assert [row.seq for row in page.messages] == [1, 2]
    assert page.cursor == sync_service.encode_cursor(result.version)


async def test_pull_with_cursor_returns_only_new_rows(env):
    first = await sync_service.push(env.alice, PushRequest(conversations=[conversation("sess_a")]))
    since = sync_service.parse_cursor(sync_service.encode_cursor(first.version))

    second = await sync_service.push(env.alice, PushRequest(messages=[message("msg_1", "sess_a")]))

    page = await sync_service.pull(env.alice, since)
    assert [row.id for row in page.conversations] == []
    assert [row.id for row in page.messages] == ["msg_1"]
    assert page.cursor == sync_service.encode_cursor(second.version)


# ---------------------------------------------------------------------
# 幂等 / 单调
# ---------------------------------------------------------------------


async def test_replaying_the_same_batch_is_a_no_op(env):
    request = PushRequest(
        conversations=[conversation("sess_a")],
        messages=[message("msg_1", "sess_a")],
    )
    first = await sync_service.push(env.alice, request)
    second = await sync_service.push(env.alice, request)
    third = await sync_service.push(env.alice, request)

    # 重试不该推进版本：否则每次重试都会唤醒所有设备的 pull
    assert second.version == first.version == third.version
    assert second.accepted.messages == 1
    assert second.rejected == []

    async with env.maker() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 1
        assert await session.scalar(select(func.count()).select_from(Conversation)) == 1


async def test_stale_client_rev_is_rejected_and_does_not_overwrite(env):
    await sync_service.push(
        env.alice,
        PushRequest(
            conversations=[conversation("sess_a")],
            messages=[message("msg_1", "sess_a", client_rev=2, payload={"content": "新"})],
        ),
    )

    result = await sync_service.push(
        env.alice,
        PushRequest(messages=[message("msg_1", "sess_a", client_rev=1, payload={"content": "旧"})]),
    )

    assert [(row.id, row.reason) for row in result.rejected] == [("msg_1", "stale")]
    page = await sync_service.pull(env.alice, 0)
    assert page.messages[0].payload == {"content": "新"}


async def test_content_update_keeps_seq_but_advances_version(env):
    await sync_service.push(
        env.alice,
        PushRequest(conversations=[conversation("sess_a")], messages=[message("msg_1", "sess_a")]),
    )
    before = await sync_service.pull(env.alice, 0)

    updated = await sync_service.push(
        env.alice,
        PushRequest(messages=[message("msg_1", "sess_a", client_rev=1, payload={"content": "流式结束后的最终内容"})]),
    )
    after = await sync_service.pull(env.alice, 0)

    # seq 是插入时定死的：流式更新不许让消息在列表里跳位
    assert after.messages[0].seq == before.messages[0].seq == 1
    assert after.messages[0].version > before.messages[0].version
    assert updated.version == after.messages[0].version
    assert after.messages[0].payload == {"content": "流式结束后的最终内容"}


# ---------------------------------------------------------------------
# 越权
# ---------------------------------------------------------------------


async def test_other_users_rows_are_invisible_and_unwritable(env):
    await sync_service.push(
        env.alice,
        PushRequest(
            conversations=[conversation("sess_a")],
            messages=[message("msg_1", "sess_a")],
        ),
    )

    conversations, messages, _ = await drain(env.bob)
    assert conversations == []
    assert messages == []

    result = await sync_service.push(
        env.bob,
        PushRequest(
            conversations=[conversation("sess_a")],
            messages=[message("msg_1", "sess_a", payload={"content": "篡改"})],
        ),
    )
    assert {(row.kind, row.reason) for row in result.rejected} == {
        ("conversation", "not_owned"),
        ("message", "not_owned"),
    }

    page = await sync_service.pull(env.alice, 0)
    assert page.messages[0].payload == {"content": "你好"}


async def test_message_for_unknown_conversation_is_rejected(env):
    result = await sync_service.push(env.alice, PushRequest(messages=[message("msg_1", "sess_missing")]))

    assert [(row.id, row.reason) for row in result.rejected] == [("msg_1", "conversation_missing")]


# ---------------------------------------------------------------------
# 删除（墓碑）
# ---------------------------------------------------------------------


async def test_tombstone_deletes_messages_and_prevents_resurrection(env):
    await sync_service.push(
        env.alice,
        PushRequest(
            conversations=[conversation("sess_a")],
            messages=[message("msg_1", "sess_a"), message("msg_2", "sess_a")],
        ),
    )
    cursor = (await sync_service.pull(env.alice, 0)).cursor

    deleted = await sync_service.push(
        env.alice, PushRequest(conversations=[conversation("sess_a", deleted=True, client_rev=1)])
    )
    assert deleted.accepted.conversations == 1

    page = await sync_service.pull(env.alice, sync_service.parse_cursor(cursor))
    assert [(row.id, row.deleted) for row in page.conversations] == [("sess_a", True)]
    # 消息随会话一起清掉，不必逐条发墓碑
    assert page.messages == []

    # 另一台设备离线时攒下的消息，不能把已经删掉的会话写回来
    late = await sync_service.push(
        env.alice,
        PushRequest(messages=[message("msg_3", "sess_a", created_at=1)], conversations=[conversation("sess_a")]),
    )
    reasons = {row.reason for row in late.rejected}
    assert reasons == {"conversation_deleted"}

    async with env.maker() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 0


# ---------------------------------------------------------------------
# 分页与游标
# ---------------------------------------------------------------------


async def test_pagination_never_skips_rows(env, monkeypatch):
    monkeypatch.setattr(settings, "sync_pull_max_rows", 2)
    await sync_service.push(env.alice, PushRequest(conversations=[conversation("sess_a")]))
    await sync_service.push(
        env.alice,
        PushRequest(messages=[message(f"msg_{index}", "sess_a") for index in range(5)]),
    )

    conversations, messages, cursor = await drain(env.alice)
    assert conversations == ["sess_a"]
    assert messages == [f"msg_{index}" for index in range(5)]

    # 追平之后再拉是空的，且游标可以继续复用
    page = await sync_service.pull(env.alice, sync_service.parse_cursor(cursor))
    assert page.conversations == [] and page.messages == []
    assert sync_service.parse_cursor(page.cursor) >= sync_service.parse_cursor(cursor)


async def test_cursor_ahead_of_server_is_rejected(env):
    with pytest.raises(sync_service.StaleCursor):
        await sync_service.pull(env.alice, 10_000)


@pytest.mark.parametrize("raw", ["", None, "v9:1", "123", "v1:abc", "v1:-1"])
def test_malformed_cursor_is_rejected(raw):
    if raw in ("", None):
        assert sync_service.parse_cursor(raw) == 0
        return
    with pytest.raises(sync_service.InvalidCursor):
        sync_service.parse_cursor(raw)


# ---------------------------------------------------------------------
# 限额：逐行拒绝，不整批失败
# ---------------------------------------------------------------------


async def test_oversized_payload_is_rejected_per_row(env, monkeypatch):
    monkeypatch.setattr(settings, "sync_payload_max_bytes", 128)
    result = await sync_service.push(
        env.alice,
        PushRequest(
            conversations=[conversation("sess_a")],
            messages=[
                message("msg_ok", "sess_a", payload={"content": "短"}),
                message("msg_big", "sess_a", payload={"content": "长" * 500}),
            ],
        ),
    )

    assert result.accepted.messages == 1
    assert [(row.id, row.reason) for row in result.rejected] == [("msg_big", "too_large")]


async def test_too_many_rows_is_a_request_error(env, monkeypatch):
    monkeypatch.setattr(settings, "sync_push_max_rows", 2)
    with pytest.raises(sync_service.TooManyRows):
        await sync_service.push(
            env.alice,
            PushRequest(
                conversations=[conversation("sess_a")],
                messages=[message("msg_1", "sess_a"), message("msg_2", "sess_a")],
            ),
        )


async def test_message_quota_rejects_new_rows_only(env, monkeypatch):
    monkeypatch.setattr(settings, "sync_max_messages_per_conversation", 2)
    await sync_service.push(
        env.alice,
        PushRequest(
            conversations=[conversation("sess_a")],
            messages=[message("msg_1", "sess_a"), message("msg_2", "sess_a")],
        ),
    )

    result = await sync_service.push(
        env.alice,
        PushRequest(
            messages=[
                message("msg_3", "sess_a"),
                # 已存在的行不计入配额，更新仍然允许
                message("msg_2", "sess_a", client_rev=1, payload={"content": "改"}),
            ]
        ),
    )

    assert [(row.id, row.reason) for row in result.rejected] == [("msg_3", "quota_exceeded")]
    assert result.accepted.messages == 1


# ---------------------------------------------------------------------
# 长轮询
# ---------------------------------------------------------------------


async def test_long_poll_wakes_up_when_another_device_pushes(env):
    await sync_service.push(env.alice, PushRequest(conversations=[conversation("sess_a")]))
    since = await sync_service.head_version(env.alice)

    waiter = asyncio.create_task(sync_service.wait_for_change(env.alice, since, timeout=10))
    await asyncio.sleep(0.05)
    assert not waiter.done()

    await sync_service.push(env.alice, PushRequest(messages=[message("msg_1", "sess_a")]))
    await asyncio.wait_for(waiter, timeout=5)


async def test_long_poll_returns_immediately_when_already_changed(env):
    await sync_service.push(env.alice, PushRequest(conversations=[conversation("sess_a")]))
    since = await sync_service.head_version(env.alice)
    await sync_service.push(env.alice, PushRequest(messages=[message("msg_1", "sess_a")]))

    started = time.monotonic()
    await sync_service.wait_for_change(env.alice, since, timeout=30)
    assert time.monotonic() - started < 1.0


async def test_long_poll_does_not_notify_other_users(env):
    await sync_service.push(env.alice, PushRequest(conversations=[conversation("sess_a")]))
    bob_since = await sync_service.head_version(env.bob)

    waiter = asyncio.create_task(sync_service.wait_for_change(env.bob, bob_since, timeout=0.3))
    await asyncio.sleep(0.05)
    await sync_service.push(env.alice, PushRequest(messages=[message("msg_1", "sess_a")]))
    await asyncio.wait_for(waiter, timeout=5)  # 靠超时结束，不会被 alice 的写入唤醒


# ---------------------------------------------------------------------
# 路由层：认证闸门（这条是「忘了挂依赖」的回归锁）
# ---------------------------------------------------------------------


async def test_sync_endpoints_require_login():
    import httpx

    from app.server import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/sync/pull")).status_code == 401
        assert (await client.post("/api/sync/push", json={"conversations": [], "messages": []})).status_code == 401


async def test_equal_rev_with_changed_content_is_applied_and_warned(env, caplog):
    """修订号没变但内容变了：客户端漏了 +1。静默丢弃会让两端永久不一致。"""
    await sync_service.push(
        env.alice,
        PushRequest(
            conversations=[conversation("sess_a", title="旧标题")],
            messages=[message("msg_1", "sess_a", payload={"content": "旧"})],
        ),
    )

    with caplog.at_level("WARNING", logger="app.services.sync_service"):
        result = await sync_service.push(
            env.alice,
            PushRequest(
                conversations=[conversation("sess_a", title="新标题")],
                messages=[message("msg_1", "sess_a", payload={"content": "新"})],
            ),
        )

    assert result.rejected == []
    page = await sync_service.pull(env.alice, 0)
    assert page.conversations[0].title == "新标题"
    assert page.messages[0].payload == {"content": "新"}
    assert "修订号未变" in " ".join(record.getMessage() for record in caplog.records)


async def test_messages_for_a_conversation_deleted_in_the_same_batch_are_rejected(env):
    result = await sync_service.push(
        env.alice,
        PushRequest(
            conversations=[conversation("sess_a", deleted=True)],
            messages=[message("msg_1", "sess_a")],
        ),
    )

    assert result.accepted.conversations == 1
    assert [(row.id, row.reason) for row in result.rejected] == [("msg_1", "conversation_deleted")]

    async with env.maker() as session:
        assert await session.scalar(select(func.count()).select_from(Message)) == 0


async def test_conversation_quota_rejects_new_rows_but_allows_updates(env, monkeypatch):
    monkeypatch.setattr(settings, "sync_max_conversations_per_user", 1)
    await sync_service.push(env.alice, PushRequest(conversations=[conversation("sess_a")]))

    result = await sync_service.push(
        env.alice,
        PushRequest(
            conversations=[
                conversation("sess_b"),
                conversation("sess_a", title="改个名", client_rev=1),
            ]
        ),
    )

    assert [(row.id, row.reason) for row in result.rejected] == [("sess_b", "quota_exceeded")]
    assert result.accepted.conversations == 1


async def test_pull_response_is_capped_by_bytes_too(env, monkeypatch):
    """行数没超但 payload 总量超了：必须分页，否则一次响应能被撑到上百 MB。"""
    await sync_service.push(env.alice, PushRequest(conversations=[conversation("sess_a")]))
    await sync_service.push(
        env.alice,
        PushRequest(
            messages=[message(f"msg_{index}", "sess_a", payload={"content": "长" * 200}) for index in range(5)]
        ),
    )
    monkeypatch.setattr(settings, "sync_pull_max_bytes", 1024)

    _, messages, _ = await drain(env.alice)
    assert messages == [f"msg_{index}" for index in range(5)]
