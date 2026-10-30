"""跨设备同步里**不碰数据库**的那部分行为锁（`app/services/sync_service.py`）。

这一层的错误方式是**静默丢数据**：客户端以为推上去了、或者以为游标已经追平，
于是某条消息在两台设备上永远对不上。

同步的核心语义（幂等 upsert、版本发号、墓碑、配额、分页）全都写在 SQL 表达式里
（`INSERT ... ON CONFLICT DO UPDATE ... RETURNING` 的行锁语义是发号器的核心），
没有可替换的仓储缝：那些用例必须有真 Postgres，已随「零基础设施」改造移除。

这里留下的是不依赖数据库就能验的部分，每一条都仍然有意义：

1. 游标是**不透明**的：格式不认识就报错，不能悄悄当成 0（客户端会拿它当位置）；
2. 行数上限在**任何写库之前**就拒绝——超限是请求级错误，不是逐行拒绝；
3. 唤醒走**端口**：注入的 notifier 可被替换、可被丢弃（多 worker 实现的前提）；
4. 路由层的**认证闸门**：没登录的 push/pull 一律 401。
"""

import time
import uuid

import pytest

from app.config import settings
from app.schemas.sync import ConversationIn, MessageIn, PushRequest
from app.services import sync_notifier, sync_service


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
