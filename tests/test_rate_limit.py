"""进程内限流（C-3）。

`/api/chat` 消耗上游额度，没有限流时任何能访问该端口的人都在花配额。

HTTP 层用例**不能真的打模型**：本机 `.env` 里配着真实网关，一旦走到生成路径就会
真发一次请求（慢、花钱、结果不确定）。所以生成器与标题生成都被替换掉了。
"""

import json
import sys
import threading
import uuid
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.api import chat as chat_api
from app.api.rate_limit import SlidingWindowLimiter, client_key, resolve_client_ip
from app.config import settings
from app.schemas.events import Done


@pytest.fixture
def fake_upstream(monkeypatch):
    """把两个会调模型的服务函数换成即时返回的实现。"""

    async def fake_stream(*_args, **_kwargs):
        yield Done()

    async def fake_title(*_args, **_kwargs):
        return "测试标题"

    monkeypatch.setattr(chat_api, "stream_chat_events", fake_stream)
    monkeypatch.setattr(chat_api, "generate_title_from_prompt", fake_title)


#: 固定的用户 id。
#:
#: 对话限流按账号计数，若依赖覆盖每次返回**新的** uuid，每个请求都会落到不同的
#: 限流键上，配额永远攒不满——用例会静默地测不出任何东西。
FIXED_USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000001")


def fake_user():
    """未落库的临时用户对象，仅用于依赖覆盖。

    这些用例只关心限流；认证本身由 `tests/test_auth.py` 覆盖。
    """
    from app.db import User

    return User(
        id=FIXED_USER_ID,
        email="rate-limit@example.com",
        hashed_password="not-a-real-hash",
        is_active=True,
        is_superuser=False,
        is_verified=False,
    )


@contextmanager
def app_client(monkeypatch, rate_limit):
    """构造 TestClient。

    必须以上下文管理器使用：sse-starlette 依赖 ASGI lifespan 初始化 `AppStatus`，
    否则 `EventSourceResponse` 的 ping 任务不会结束。

    `/api/*` 要求登录，这里把 `current_active_user` 替换成固定用户。被替换的只是
    **登录态判断**，路由与限流依赖仍会真实执行——这样限流用例挂掉时，原因只可能是限流。
    """
    monkeypatch.setattr(settings, "chat_rate_limit_per_minute", rate_limit)

    from app.server import create_app
    from app.services.auth import current_active_user

    app = create_app()
    app.dependency_overrides[current_active_user] = fake_user

    with TestClient(app) as client:
        yield client


def valid_body():
    return {"messages": [{"role": "user", "content": "你好"}]}


@contextmanager
def auth_client(monkeypatch, auth_rate_limit):
    """**未登录**的 client，用于认证端点的 IP 限流。

    不做依赖覆盖：认证限流本身就是被测对象，必须在真实（未登录）状态下测。
    """
    monkeypatch.setattr(settings, "auth_rate_limit_per_minute", auth_rate_limit)

    from app.server import create_app

    with TestClient(create_app()) as client:
        yield client


def login_body():
    return {"username": "nobody@example.com", "password": "hunter2hunter2"}


def register_body():
    return {
        "email": f"rl-{uuid.uuid4().hex[:10]}@example.com",
        "password": "hunter2hunter2",
    }


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


def test_eviction_prefers_expired_key_over_still_usable_key(monkeypatch):
    """容量满时先丢窗口内已无命中的键，而不是丢掉还能继续用的键。"""
    clock = {"now": 1000.0}
    monkeypatch.setattr("app.api.rate_limit.time.monotonic", lambda: clock["now"])

    limiter = SlidingWindowLimiter(limit=5, window_seconds=10, max_keys=2)
    limiter.check("stale")
    clock["now"] = 1016.0
    limiter.check("live")

    clock["now"] = 1020.0  # cutoff=1010：stale 已过期，live 仍在窗口内
    limiter.check("newcomer")

    assert "stale" not in limiter._hits
    assert "live" in limiter._hits


def test_lru_eviction_never_resets_a_banned_key():
    """max_keys 小时 churn 新 key 不能重置已封禁的键。

    已封禁的键命中停留在封禁那一刻，按「最近命中时间」淘汰它恰好最旧，于是依次插入
    几个新 key 就能消掉自己的封禁。
    """
    limiter = SlidingWindowLimiter(limit=3, window_seconds=60, max_keys=2)

    assert [limiter.check("victim")[0] for _ in range(3)] == [True, True, True]
    assert limiter.check("victim")[0] is False  # 已封禁
    assert limiter.check("filler")[0] is True

    # 表满：该淘汰未达限的 filler，而不是把封禁中的 victim 丢掉
    assert limiter.check("newcomer")[0] is True

    assert "victim" in limiter._hits
    assert limiter.check("victim")[0] is False


def test_zero_or_negative_limit_means_unlimited():
    """与 settings 的「0=关闭」一致：limit <= 0 直接放行，而不是全拒绝。"""
    limiter = SlidingWindowLimiter(limit=0, window_seconds=60)
    assert limiter.check("ip") == (True, 0.0)

    limiter_neg = SlidingWindowLimiter(limit=-5, window_seconds=30)
    assert limiter_neg.check("ip") == (True, 0.0)
    # 不限流时不该白白记录状态
    assert limiter_neg._hits == {}


@pytest.mark.parametrize("max_keys", [0, -1])
def test_max_keys_must_be_positive(max_keys):
    with pytest.raises(ValueError):
        SlidingWindowLimiter(limit=1, window_seconds=60, max_keys=max_keys)


@pytest.mark.parametrize("window_seconds", [0, -1.0])
def test_window_seconds_must_be_positive(window_seconds):
    with pytest.raises(ValueError):
        SlidingWindowLimiter(limit=1, window_seconds=window_seconds)


def test_hit_exactly_on_window_boundary_is_expired(monkeypatch):
    """窗口是 `(now - window, now]`：恰好落在左端点的命中已经过期，不该继续占配额。"""
    clock = {"now": 500.0}
    monkeypatch.setattr("app.api.rate_limit.time.monotonic", lambda: clock["now"])

    limiter = SlidingWindowLimiter(limit=1, window_seconds=10)
    assert limiter.check("ip")[0] is True

    clock["now"] = 509.999
    assert limiter.check("ip")[0] is False

    clock["now"] = 510.0
    assert limiter.check("ip")[0] is True


def test_concurrent_checks_are_safe():
    """并发冒烟：多线程混跑下不抛异常、追踪表上限依然成立。

    真实调用来自 asyncio 事件循环（check 是纯同步函数，单线程内天然原子），这里再加
    一层线程压力。
    """
    limiter = SlidingWindowLimiter(limit=5, window_seconds=60, max_keys=3)
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker(worker_id: int) -> None:
        try:
            barrier.wait()
            for index in range(200):
                limiter.check(f"ip-{worker_id % 3}-{index % 7}")
        except BaseException as exc:  # pragma: no cover - 只有并发 bug 才走到
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(worker_id,)) for worker_id in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(limiter._hits) <= 3


def test_resolve_client_ip():
    """验证反向代理 IP 识别逻辑。"""
    # 无受信任标头时返回直连 IP
    assert resolve_client_ip("127.0.0.1", headers={"x-forwarded-for": "10.0.0.1"}) == "127.0.0.1"

    # 有受信任标头时取**最右**值：那是最后一跳代理追加的真实来源
    trusted = ["X-Forwarded-For", "X-Real-IP"]
    headers = {"X-Forwarded-For": "203.0.113.195, 70.41.3.18, 150.172.238.178"}
    assert resolve_client_ip("127.0.0.1", headers=headers, trusted_headers=trusted) == "150.172.238.178"

    headers_real_ip = {"X-Real-IP": "198.51.100.1"}
    assert resolve_client_ip("127.0.0.1", headers=headers_real_ip, trusted_headers=trusted) == "198.51.100.1"

    # headers 为空或标头值全为空时回退直连 IP
    assert resolve_client_ip("127.0.0.1", headers={}, trusted_headers=trusted) == "127.0.0.1"
    assert resolve_client_ip("127.0.0.1", headers={"X-Forwarded-For": "  "}, trusted_headers=trusted) == "127.0.0.1"
    assert resolve_client_ip(None, headers={}, trusted_headers=trusted) == "unknown"


def test_client_forged_xff_leftmost_is_ignored():
    """客户端可以把最左值写成任意内容；只有代理追加的最右值能决定限流键。"""
    trusted = ["X-Forwarded-For"]
    forged = {"X-Forwarded-For": "6.6.6.6, 7.7.7.7, 203.0.113.9"}

    assert resolve_client_ip("127.0.0.1", headers=forged, trusted_headers=trusted) == "203.0.113.9"

    # 代理异常或手工拼接会在末尾留下空段：取最右**非空**值
    trailing = {"X-Forwarded-For": "203.0.113.9, , "}
    assert resolve_client_ip("127.0.0.1", headers=trailing, trusted_headers=trusted) == "203.0.113.9"
    assert resolve_client_ip("127.0.0.1", headers={"X-Forwarded-For": " , "}, trusted_headers=trusted) == "127.0.0.1"


def test_reset_clears_state():
    limiter = SlidingWindowLimiter(limit=1, window_seconds=60)
    limiter.check("ip")

    limiter.reset()

    assert limiter.check("ip")[0] is True


def test_client_key_falls_back_for_missing_host():
    """缺来源时归入同一个桶，宁可保守也不要放过。"""
    assert client_key(None) == "unknown"
    assert client_key("1.2.3.4") == "1.2.3.4"


def test_client_key_scope_splits_buckets_per_endpoint():
    """同一 IP 的 login / register 是两个独立桶。

    共用一个月会造出一个很难解释的失败模式：有人在一个 NAT 出口后面猛试密码，把
    10 次/分钟打满之后，同一出口的**正常注册**也做不了（反之亦然）。
    """
    assert client_key("1.2.3.4", "login") != client_key("1.2.3.4", "register")
    assert client_key("1.2.3.4", "login") == "login:1.2.3.4"
    # 无 scope 时保持旧行为（对话端点等调用点不受影响）
    assert client_key("1.2.3.4") == "1.2.3.4"


def test_register_is_not_blocked_by_exhausted_login_bucket(monkeypatch):
    """端到端：把一个 IP 的登录额度打满，它仍然能注册。"""
    with auth_client(monkeypatch, auth_rate_limit=2) as client:
        bad_login = login_body()
        assert client.post("/api/auth/login", data=bad_login).status_code == 400
        assert client.post("/api/auth/login", data=bad_login).status_code == 400
        # 第三次登录被限流
        assert client.post("/api/auth/login", data=bad_login).status_code == 429

        # 关键断言：同一 IP 注册**未**被牵连（修复前这里是 429）
        registered = client.post("/api/auth/register", json=register_body())
        assert registered.status_code == 201, registered.text


# ---------------------------------------------------------------------
# 接线到 HTTP 层
# ---------------------------------------------------------------------
def test_returns_429_with_retry_after(monkeypatch, fake_upstream):
    with app_client(monkeypatch, rate_limit=2) as client:
        first = client.post("/api/chat", json=valid_body())
        assert first.status_code == 200
        # SSE 契约：Content-Type 正确，且（生成器已被 mock 成即时 Done）首帧是可解析的 data 帧
        assert first.headers["content-type"].startswith("text/event-stream")
        first_frame = next(line for line in first.text.splitlines() if line.startswith("data: "))
        assert json.loads(first_frame.removeprefix("data: ")) == {"type": "done"}

        assert client.post("/api/chat", json=valid_body()).status_code == 200

        response = client.post("/api/chat", json=valid_body())

    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1
    # 对话 429 同样要带标准配额头（与认证端点一致），否则客户端读不到配额
    assert response.headers["X-RateLimit-Limit"] == "2"
    assert response.headers["X-RateLimit-Remaining"] == "0"
    assert int(response.headers["X-RateLimit-Reset"]) >= 1
    assert "请求过于频繁" in response.json()["detail"]


def test_title_endpoint_is_limited_too(monkeypatch, fake_upstream):
    """标题生成也会调模型，同样在成本路径上。"""
    with app_client(monkeypatch, rate_limit=1) as client:
        assert client.post("/api/chat/title", json={"prompt": "你好"}).status_code == 200
        assert client.post("/api/chat/title", json={"prompt": "你好"}).status_code == 429


#: `SSE_HEADERS` 里没有 `Connection` 这件事不在字典上断言：
#: sse-starlette 会在合并调用方 headers **之后**无条件写入 `Connection: keep-alive`，
#: 只断言字典是伪保证。真正剥掉它的是 `ChatEventSourceResponse`，断言在
#: `tests/test_events.py::test_chat_sse_response_headers` 的真实响应上。


def test_invalid_body_still_consumes_quota(monkeypatch):
    """非法请求体也要计数，否则可以拿无效请求绕过限流。

    这条也顺便证明：不用真的调模型就能验证限流。
    """
    with app_client(monkeypatch, rate_limit=1) as client:
        assert client.post("/api/chat", json={"messages": []}).status_code == 422
        assert client.post("/api/chat", json={"messages": []}).status_code == 429


def test_models_endpoint_is_not_limited(monkeypatch):
    """模型列表有缓存、不打模型，不需要和对话共享配额。

    必须把 `fetch_available_models` 换掉：本机 `.env` 指向真实网关，真调用会打上游
    （慢、花钱）并把结果写进 model_service 的模块级缓存，污染其他用例。
    """

    async def fake_fetch(*_args, **_kwargs):
        return {
            "models": [{"id": "test/model", "name": "test", "provider": "test"}],
            "default_model": "test/model",
        }

    monkeypatch.setattr(chat_api, "fetch_available_models", fake_fetch)

    with app_client(monkeypatch, rate_limit=1) as client:
        first = client.get("/api/models")
        assert first.status_code == 200
        # 对话配额只有 1 也不影响模型列表：它不共享对话桶
        assert client.get("/api/models").status_code == 200

    assert first.json()["default_model"] == "test/model"


def test_chat_quota_is_per_account_not_per_ip(monkeypatch, fake_upstream):
    """对话配额按**账号**计，换 IP 换不来新配额。

    按 IP 计数在前端同源代理（开发期 Vite、生产 Nginx）下会退化成「全站」——后端看到的
    `client.host` 永远是代理地址，一个人打满就挡住所有人。
    """
    monkeypatch.setattr(settings, "trusted_proxy_headers", "X-Forwarded-For")
    with app_client(monkeypatch, rate_limit=1) as client:
        # 最左值是客户端伪造的，最右值是代理追加的真实 IP（对话桶按账号，XFF 无关）
        first = client.post("/api/chat", json=valid_body(), headers={"X-Forwarded-For": "198.51.100.7, 192.0.2.1"})
        assert first.status_code == 200

        # 同一账号、不同 IP：仍应被拦
        second = client.post("/api/chat", json=valid_body(), headers={"X-Forwarded-For": "198.51.100.8, 192.0.2.2"})
        assert second.status_code == 429


def test_zero_disables_limiting(monkeypatch, fake_upstream):
    with app_client(monkeypatch, rate_limit=0) as client:
        for _ in range(5):
            assert client.post("/api/chat", json=valid_body()).status_code == 200


# ---------------------------------------------------------------------
# 认证端点限流（按 IP）
# ---------------------------------------------------------------------
def test_login_brute_force_is_blocked(monkeypatch):
    """密码暴力破解必须被挡住：认证端点同样在成本路径上。"""
    with auth_client(monkeypatch, auth_rate_limit=3) as client:
        for _ in range(3):
            assert client.post("/api/auth/login", data=login_body()).status_code == 400

        response = client.post("/api/auth/login", data=login_body())

    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1
    # 标准配额头：客户端与监控据此知道配额上限与剩余量
    assert response.headers["X-RateLimit-Limit"] == "3"
    assert response.headers["X-RateLimit-Remaining"] == "0"
    assert int(response.headers["X-RateLimit-Reset"]) >= 1


def test_register_is_rate_limited(monkeypatch):
    """批量注册同样按 IP 挡。"""
    with auth_client(monkeypatch, auth_rate_limit=1) as client:
        assert client.post("/api/auth/register", json=register_body()).status_code == 201
        assert client.post("/api/auth/register", json=register_body()).status_code == 429


def test_auth_quota_is_per_ip(monkeypatch):
    """认证配额按 IP 计：这里攻击者换的是来源，目标是常量。

    两处请求的 XFF 最左值都是客户端伪造的同一个值，真实来源由代理追加在最右。
    """
    monkeypatch.setattr(settings, "trusted_proxy_headers", "X-Forwarded-For")
    with auth_client(monkeypatch, auth_rate_limit=1) as client:
        first = client.post(
            "/api/auth/login",
            data=login_body(),
            headers={"X-Forwarded-For": "198.51.100.66, 192.0.2.1"},
        )
        assert first.status_code == 400

        # 另一个真实来源有独立配额（伪造的最左值相同也不影响）
        other = client.post(
            "/api/auth/login",
            data=login_body(),
            headers={"X-Forwarded-For": "198.51.100.66, 192.0.2.2"},
        )
        assert other.status_code == 400

        # 同一真实来源超限
        repeat = client.post(
            "/api/auth/login",
            data=login_body(),
            headers={"X-Forwarded-For": "198.51.100.66, 192.0.2.1"},
        )
        assert repeat.status_code == 429


def test_auth_forged_xff_leftmost_does_not_create_a_bucket(monkeypatch):
    """换一个伪造的最左值不该换来新桶——否则限流键由客户端自选，等于没有限流。"""
    monkeypatch.setattr(settings, "trusted_proxy_headers", "X-Forwarded-For")
    with auth_client(monkeypatch, auth_rate_limit=1) as client:
        first = client.post("/api/auth/login", data=login_body(), headers={"X-Forwarded-For": "6.6.6.6, 192.0.2.7"})
        assert first.status_code == 400

        forged = client.post("/api/auth/login", data=login_body(), headers={"X-Forwarded-For": "9.9.9.9, 192.0.2.7"})
        assert forged.status_code == 429


def test_logout_is_not_rate_limited(monkeypatch):
    """登出不能被限流卡住：用户被限流后连登出都做不了。"""
    with auth_client(monkeypatch, auth_rate_limit=1) as client:
        # 先耗尽登录配额
        client.post("/api/auth/login", data=login_body())
        assert client.post("/api/auth/login", data=login_body()).status_code == 429

        # 登出不受影响：未登录时是 401（缺登录态），不是 429（被限流）
        assert client.post("/api/auth/logout").status_code == 401


def test_auth_limiting_can_be_disabled(monkeypatch):
    with auth_client(monkeypatch, auth_rate_limit=0) as client:
        for _ in range(5):
            assert client.post("/api/auth/login", data=login_body()).status_code == 400


# ---------------------------------------------------------------------
# 启动参数（main.py）
# ---------------------------------------------------------------------
#: 这几个用例只依赖 argparse 与 uvicorn 调用，和限流无关；没有单独的 test_main.py，
#: 放在这里是因为本文件已持有启动参数相关用例。
def stub_uvicorn_run(monkeypatch) -> list[dict]:
    """拦下 uvicorn.run，避免用例真的起服务；返回调用记录。"""
    import uvicorn

    calls: list[dict] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append({"args": args, "kwargs": kwargs}))
    return calls


@pytest.mark.parametrize("port", ["0", "65536", "-1"])
def test_out_of_range_port_is_rejected(monkeypatch, port):
    import main as main_module

    monkeypatch.setattr(sys, "argv", ["main.py", "--web", "--port", port])
    calls = stub_uvicorn_run(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        main_module.main()

    assert excinfo.value.code == 2  # argparse 的用法错误退出码
    assert calls == []  # 校验发生在启动之前


@pytest.mark.parametrize("port", ["1", "65535"])
def test_boundary_ports_are_accepted(monkeypatch, port):
    import main as main_module

    monkeypatch.setattr(sys, "argv", ["main.py", "--web", "--port", port])
    calls = stub_uvicorn_run(monkeypatch)

    main_module.main()

    assert calls[0]["kwargs"]["port"] == int(port)


def test_web_mode_disables_uvicorn_proxy_headers(monkeypatch):
    """uvicorn 默认 proxy_headers=True 且信任 loopback，可凭 XFF 改写 client.host。

    那会绕过本模块「只有显式配置 trusted_proxy_headers 才信标头」的模型，等于又让客户端
    自选限流键，所以必须在启动参数里显式关掉。
    """
    import main as main_module

    monkeypatch.setattr(sys, "argv", ["main.py", "--web", "--port", "9001"])
    calls = stub_uvicorn_run(monkeypatch)

    main_module.main()

    assert len(calls) == 1
    assert calls[0]["kwargs"]["proxy_headers"] is False
