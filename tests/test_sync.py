"""跨设备同步里**不碰数据库**的那部分行为锁（`app/services/sync_service.py`）。

这一层的错误方式是**静默丢数据**：客户端以为推上去了、或者以为游标已经追平，
于是某条消息在两台设备上永远对不上。

同步的核心语义（幂等 upsert、版本发号、墓碑、配额、分页）全都写在 SQL 表达式里
（`INSERT ... ON CONFLICT DO UPDATE ... RETURNING` 的行锁语义是发号器的核心），
没有可替换的仓储缝：那些用例必须有真 Postgres，已随「零基础设施」改造移除。

这里留下的是不依赖数据库就能验的部分，每一条都仍然有意义：

1. 游标是**不透明**的：格式不认识就报错，不能悄悄当成 0（客户端会拿它当位置）；
2. 行数上限在**任何写库之前**就拒绝——超限是请求级错误，不是逐行拒绝；
3. 唤醒走**端口**：注入的 notifier 可被替换、可被丢弃（多 worker 实现的前提），且
   同一用户的多个等待方互不吞唤醒；
4. 路由层的**认证闸门**：没登录的 push/pull 一律 401；
5. push 的串行化是**按用户**的数据库锁，不是全进程单锁（否则一个人推大 batch 会
   让全站排队）；
6. 拒绝与游标失效都有**指标**：这两类故障此前在服务端完全不可见。
"""

import time
import uuid
from types import SimpleNamespace

import pytest

from app.config import settings
from app.schemas.sync import ConversationIn, MessageIn, PushRequest, RejectedRow
from app.services import sync_notifier, sync_service
from app.services.metrics import metrics


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


# ---------------------------------------------------------------------
# 游标：不透明，解析失败必须报错
# ---------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["", None, "v9:1", "123", "v1:abc", "v1:-1"])
def test_malformed_cursor_is_rejected(raw):
    if raw in ("", None):
        assert sync_service.parse_cursor(raw) == 0
        return
    with pytest.raises(sync_service.InvalidCursor):
        sync_service.parse_cursor(raw)


def test_cursor_round_trips_through_the_opaque_encoding():
    """游标是给客户端原样回传的不透明串：编解码必须互逆。"""
    assert sync_service.parse_cursor(sync_service.encode_cursor(42)) == 42
    assert sync_service.encode_cursor(42) != "42"


# ---------------------------------------------------------------------
# 限额：超限在写库之前拒绝
# ---------------------------------------------------------------------


async def test_too_many_rows_is_a_request_error(monkeypatch):
    """单次推送行数超限 → 整次请求失败，客户端按 `SYNC_PUSH_MAX_ROWS` 分批。

    这条不需要数据库：行数判断发生在任何会话打开之前，所以它是「请求级错误」而不是
    「逐行拒绝」——两种语义在客户端的处理方式完全不同，不能混。
    """
    monkeypatch.setattr(settings, "sync_push_max_rows", 2)
    with pytest.raises(sync_service.TooManyRows):
        await sync_service.push(
            uuid.uuid4(),
            PushRequest(
                conversations=[conversation("sess_a")],
                messages=[message("msg_1", "sess_a"), message("msg_2", "sess_a")],
            ),
        )


# ---------------------------------------------------------------------
# 唤醒端口：可注入、可丢弃
# ---------------------------------------------------------------------


def test_reset_notifier_puts_a_fresh_single_process_notifier_back():
    """旧入口仍管用：注入的 notifier 被丢掉，下次取到的是全新的单进程实现。"""
    injected = sync_notifier.InMemoryNotifier()
    sync_notifier.set_notifier_for_tests(injected)

    sync_service.reset_notifier()

    assert sync_notifier.get_notifier() is not injected


#: 等待方拿到唤醒后应远早于 `wait()` 的上限返回；被吞掉时必然等到满额。
_WAIT_BUDGET = 1.0
_EARLY_RETURN = _WAIT_BUDGET / 2


async def _wait_elapsed(subscription: sync_notifier.ChangeSubscription) -> float:
    started = time.monotonic()
    await subscription.wait(_WAIT_BUDGET)
    return time.monotonic() - started


async def test_late_subscriber_does_not_swallow_an_earlier_wake():
    """P2 回归：同一用户的多个等待方共享一个 `Event` 时，后进入者会吞掉先进入者的唤醒。

    旧实现 `subscribe()` 一进来就 `event.clear()`。设备 A 已经注册好、还没 `wait()` 时，设备 B
    的新长轮询进来 clear 一次，把这期间到达的 `notify` 抹掉：A 白等一个 25s 周期（数据不丢，
    但「毫秒级唤醒」退化成「每轮都靠超时」）。
    """
    notifier = sync_notifier.InMemoryNotifier()
    user_id = uuid.uuid4()

    async with notifier.subscribe(user_id) as first:
        notifier.notify(user_id)
        # 第二个等待方此时才进来：它不能影响第一个已经收到的唤醒
        async with notifier.subscribe(user_id) as second:
            assert await _wait_elapsed(first) < _EARLY_RETURN
            # 之后的唤醒照常送到第二个等待方
            notifier.notify(user_id)
            assert await _wait_elapsed(second) < _EARLY_RETURN


async def test_one_notify_wakes_every_waiter_of_the_user():
    """一次写入要把该用户所有挂着的长轮询都唤醒，而不是只叫醒一个。"""
    notifier = sync_notifier.InMemoryNotifier()
    user_id = uuid.uuid4()

    async with notifier.subscribe(user_id) as first, notifier.subscribe(user_id) as second:
        notifier.notify(user_id)

        assert await _wait_elapsed(first) < _EARLY_RETURN
        assert await _wait_elapsed(second) < _EARLY_RETURN


# ---------------------------------------------------------------------
# push 的串行化：按用户，不是全进程
# ---------------------------------------------------------------------


class _RecordingSession:
    """只记下 `execute` 的语句与参数：不需要真数据库就能验「锁是按 user_id 取的」。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []

    async def execute(self, statement, parameters=None):  # noqa: ANN001
        self.calls.append((str(statement), parameters))
        return None


async def test_push_lock_is_keyed_by_user_id():
    """锁必须按 user_id 取：之前那把无 key 的模块级 `asyncio.Lock` 会让任一用户的 200 行
    大 batch 期间全站 push 排队，而它保护的配额本来就是 per-user 的。

    `pg_advisory_xact_lock` 还是**跨进程**的（多 worker 也生效），且在事务结束时自动释放。
    """
    user_id = uuid.uuid4()
    session = _RecordingSession()

    await sync_service._lock_user(session, user_id)  # noqa: SLF001

    assert len(session.calls) == 1
    statement, parameters = session.calls[0]
    assert "pg_advisory_xact_lock" in statement
    assert "hashtext(:user_id)" in statement
    assert parameters == {"user_id": str(user_id)}


def test_push_has_no_process_wide_lock_any_more():
    """回归锁：再往这里加一把全局 `asyncio.Lock`，就得先解释清楚它的 key 是什么。"""
    assert not hasattr(sync_service, "_get_push_lock")


# ---------------------------------------------------------------------
# 拒绝与游标失效的指标
# ---------------------------------------------------------------------


def test_push_rejections_are_counted_by_reason():
    """拒绝原因是有限枚举，可以安全当标签：没有它，`stale` 风暴只有用户报障才看得见。"""
    rejections = [
        RejectedRow(kind="conversation", id="sess_a", reason="stale"),
        RejectedRow(kind="conversation", id="sess_b", reason="stale"),
        RejectedRow(kind="message", id="msg_1", reason="too_large"),
    ]
    stale_before = metrics.counter_value(sync_service.SYNC_PUSH_REJECTED_TOTAL, reason="stale")
    large_before = metrics.counter_value(sync_service.SYNC_PUSH_REJECTED_TOTAL, reason="too_large")

    sync_service._record_rejections(rejections)  # noqa: SLF001

    assert metrics.counter_value(sync_service.SYNC_PUSH_REJECTED_TOTAL, reason="stale") == stale_before + 2
    assert metrics.counter_value(sync_service.SYNC_PUSH_REJECTED_TOTAL, reason="too_large") == large_before + 1


async def test_stale_cursor_is_reported_as_410_and_counted(monkeypatch):
    """410 是客户端「丢游标重拉」的唯一入口，也是服务端唯一能观测到游标失效的地方。"""
    from fastapi import HTTPException

    from app.api import sync as sync_api

    async def _stale(*_args, **_kwargs):
        raise sync_service.StaleCursor("游标 5 超出服务端当前版本 3")

    monkeypatch.setattr(sync_service, "pull", _stale)
    before = metrics.counter_value(sync_service.SYNC_CURSOR_STALE_TOTAL)

    with pytest.raises(HTTPException) as raised:
        await sync_api.pull(cursor="v1:5", limit=None, timeout=0, user=SimpleNamespace(id=uuid.uuid4()))

    assert raised.value.status_code == 410
    assert metrics.counter_value(sync_service.SYNC_CURSOR_STALE_TOTAL) == before + 1


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
