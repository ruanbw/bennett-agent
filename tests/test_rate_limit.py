"""进程内限流（C-3）。

`/api/chat` 允许匿名调用，没有限流时任何能访问该端口的人都在消耗上游额度。
默认监听地址已改回环，但那只挡住「不是本机的人」。

HTTP 层用例**不能真的打模型**：本机 `.env` 里配着真实网关，一旦走到生成路径就会
真发一次请求（慢、花钱、结果不确定）。所以生成器与标题生成都被替换掉了。
"""

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.api import chat as chat_api
from app.api.rate_limit import SlidingWindowLimiter, client_key, resolve_client_ip
from app.config import settings
from app.schemas.events import Done


@pytest.fixture(autouse=True)
def fresh_limiter():
    """限流器是模块级状态，用例之间必须隔离。"""
    chat_api._limiter = None
    yield
    chat_api._limiter = None


@pytest.fixture
def fake_upstream(monkeypatch):
    """把两个会调模型的服务函数换成即时返回的实现。"""

    async def fake_stream(*_args, **_kwargs):
        yield Done()

    async def fake_title(*_args, **_kwargs):
        return "测试标题"

    monkeypatch.setattr(chat_api, "stream_chat_events", fake_stream)
    monkeypatch.setattr(chat_api, "generate_title_from_prompt", fake_title)


@contextmanager
def app_client(monkeypatch, rate_limit):
    """构造 TestClient。

    必须以上下文管理器使用：sse-starlette 依赖 ASGI lifespan 初始化 `AppStatus`，
    否则 `EventSourceResponse` 的 ping 任务不会结束。
    """
    monkeypatch.setattr(settings, "chat_rate_limit_per_minute", rate_limit)

    from app.server import create_app

    with TestClient(create_app()) as client:
        yield client


def valid_body():
    return {"messages": [{"role": "user", "content": "你好"}]}


# ---------------------------------------------------------------------
# 限流器本身
# ---------------------------------------------------------------------
def test_allows_up_to_limit():
    limiter = SlidingWindowLimiter(limit=3, window_seconds=60)

    assert [limiter.check("ip")[0] for _ in range(3)] == [True, True, True]


def test_blocks_beyond_limit_and_suggests_retry_after():
    limiter = SlidingWindowLimiter(limit=2, window_seconds=60)
    limiter.check("ip")
    limiter.check("ip")

    allowed, retry_after = limiter.check("ip")

    assert allowed is False
    assert 0 < retry_after <= 60


def test_keys_are_independent():
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60)
    limiter.check("a")

    assert limiter.check("b")[0] is True
    assert limiter.check("a")[0] is False


def test_window_slides(monkeypatch):
    """窗口之外的命中要被丢掉，否则限流器会永久锁死一个来源。"""
    clock = {"now": 1000.0}
    monkeypatch.setattr("app.api.rate_limit.time.monotonic", lambda: clock["now"])

    limiter = SlidingWindowLimiter(limit=1, window_seconds=10)
    assert limiter.check("ip")[0] is True
    assert limiter.check("ip")[0] is False

    clock["now"] += 11
    assert limiter.check("ip")[0] is True


def test_tracked_keys_are_capped():
    """伪造来源不能把内存吃满。"""
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60, max_keys=5)

    for index in range(20):
        limiter.check(f"ip-{index}")

    assert len(limiter._hits) <= 5


def test_tracked_keys_evict_expired_and_lru():
    """超限时不应清空全表，而是清理过期项或淘汰最旧项。"""
    limiter = SlidingWindowLimiter(limit=5, window_seconds=10, max_keys=3)

    limiter.check("ip-1")
    limiter.check("ip-2")
    limiter.check("ip-3")

    # 4th key 触发淘汰，由于未过期，最久未访问的 ip-1 应被淘汰，ip-2 和 ip-3 保留
    limiter.check("ip-4")
    assert len(limiter._hits) <= 3
    assert "ip-1" not in limiter._hits
    assert "ip-2" in limiter._hits
    assert "ip-3" in limiter._hits
    assert "ip-4" in limiter._hits


def test_zero_or_negative_limit_safe():
    """limit <= 0 增加安全性防御，不触发 IndexError。"""
    limiter = SlidingWindowLimiter(limit=0, window_seconds=60)
    allowed, retry_after = limiter.check("ip")
    assert allowed is False
    assert retry_after == 60.0

    limiter_neg = SlidingWindowLimiter(limit=-5, window_seconds=30)
    allowed, retry_after = limiter_neg.check("ip")
    assert allowed is False
    assert retry_after == 30.0


def test_resolve_client_ip():
    """验证反向代理 IP 识别逻辑。"""
    # 无受信任标头时返回直连 IP
    assert resolve_client_ip("127.0.0.1", headers={"x-forwarded-for": "10.0.0.1"}) == "127.0.0.1"

    # 有受信任标头时优先提取真实 IP（取第一个）
    trusted = ["X-Forwarded-For", "X-Real-IP"]
    headers = {"X-Forwarded-For": "203.0.113.195, 70.41.3.18, 150.172.238.178"}
    assert resolve_client_ip("127.0.0.1", headers=headers, trusted_headers=trusted) == "203.0.113.195"

    headers_real_ip = {"X-Real-IP": "198.51.100.1"}
    assert resolve_client_ip("127.0.0.1", headers=headers_real_ip, trusted_headers=trusted) == "198.51.100.1"

    # headers 为空或标头值全为空时回退直连 IP
    assert resolve_client_ip("127.0.0.1", headers={}, trusted_headers=trusted) == "127.0.0.1"
    assert resolve_client_ip("127.0.0.1", headers={"X-Forwarded-For": "  "}, trusted_headers=trusted) == "127.0.0.1"
    assert resolve_client_ip(None, headers={}, trusted_headers=trusted) == "unknown"


def test_reset_clears_state():
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60)
    limiter.check("ip")

    limiter.reset()

    assert limiter.check("ip")[0] is True


def test_client_key_falls_back_for_missing_host():
    """缺来源时归入同一个桶，宁可保守也不要放过。"""
    assert client_key(None) == "unknown"
    assert client_key("1.2.3.4") == "1.2.3.4"


# ---------------------------------------------------------------------
# 接线到 HTTP 层
# ---------------------------------------------------------------------
def test_returns_429_with_retry_after(monkeypatch, fake_upstream):
    with app_client(monkeypatch, rate_limit=2) as client:
        assert client.post("/api/chat", json=valid_body()).status_code == 200
        assert client.post("/api/chat", json=valid_body()).status_code == 200

        response = client.post("/api/chat", json=valid_body())

    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1
    assert "请求过于频繁" in response.json()["detail"]


def test_title_endpoint_is_limited_too(monkeypatch, fake_upstream):
    """标题生成也会调模型，同样在成本路径上。"""
    with app_client(monkeypatch, rate_limit=1) as client:
        assert client.post("/api/chat/title", json={"prompt": "你好"}).status_code == 200
        assert client.post("/api/chat/title", json={"prompt": "你好"}).status_code == 429


def test_invalid_body_still_consumes_quota(monkeypatch):
    """非法请求体也要计数，否则可以拿无效请求绕过限流。

    这条也顺便证明：不用真的调模型就能验证限流。
    """
    with app_client(monkeypatch, rate_limit=1) as client:
        assert client.post("/api/chat", json={"messages": []}).status_code == 422
        assert client.post("/api/chat", json={"messages": []}).status_code == 429


def test_models_endpoint_is_not_limited(monkeypatch):
    """模型列表有缓存、不打模型，不需要和对话共享配额。"""
    with app_client(monkeypatch, rate_limit=1) as client:
        assert client.get("/api/models").status_code == 200
        assert client.get("/api/models").status_code == 200


def test_trusted_proxy_header_applied_in_chat(monkeypatch, fake_upstream):
    """配置 trusted_proxy_headers 时，Chat 限流应识别反向代理转发的真实 IP。"""
    monkeypatch.setattr(settings, "trusted_proxy_headers", "X-Forwarded-For")
    with app_client(monkeypatch, rate_limit=1) as client:
        # 不同客户端 IP 独立限流
        res1 = client.post(
            "/api/chat",
            json=valid_body(),
            headers={"X-Forwarded-For": "192.0.2.1"},
        )
        assert res1.status_code == 200

        res2 = client.post(
            "/api/chat",
            json=valid_body(),
            headers={"X-Forwarded-For": "192.0.2.2"},
        )
        assert res2.status_code == 200

        # 同一客户端 IP 超限
        res3 = client.post(
            "/api/chat",
            json=valid_body(),
            headers={"X-Forwarded-For": "192.0.2.1"},
        )
        assert res3.status_code == 429


def test_zero_disables_limiting(monkeypatch, fake_upstream):
    with app_client(monkeypatch, rate_limit=0) as client:
        for _ in range(5):
            assert client.post("/api/chat", json=valid_body()).status_code == 200
