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
from app.api.rate_limit import SlidingWindowLimiter, client_key
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


def test_zero_disables_limiting(monkeypatch, fake_upstream):
    with app_client(monkeypatch, rate_limit=0) as client:
        for _ in range(5):
            assert client.post("/api/chat", json=valid_body()).status_code == 200
