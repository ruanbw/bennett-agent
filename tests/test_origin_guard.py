"""跨站请求守卫（M-01 登录 CSRF 回归锁）。

登录是表单编码的简单请求，SameSite=Lax 不会阻止跨站顶层 POST 写入新 cookie。
`OriginGuardMiddleware` 对 `/api/*` 的非安全方法做 Origin/Referer 校验。
"""

import logging

from app import server
from app.middleware import OriginGuardMiddleware
from app.services.metrics import metrics


async def _run(
    origin=None,
    referer=None,
    sec_fetch_site=None,
    method="POST",
    path="/api/auth/login",
    host="app.test",
    allowed_origins=("https://frontend.example",),
    allow_any_origin=False,
):
    calls = {"app": False}
    sent: list[dict] = []

    async def app(scope, receive, send):  # noqa: ANN001
        calls["app"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = OriginGuardMiddleware(app, allowed_origins=list(allowed_origins), allow_any_origin=allow_any_origin)
    headers = [(b"host", host.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if referer is not None:
        headers.append((b"referer", referer.encode()))
    if sec_fetch_site is not None:
        headers.append((b"sec-fetch-site", sec_fetch_site.encode()))
    scope = {"type": "http", "method": method, "path": path, "headers": headers}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await middleware(scope, receive, send)
    status = next(message["status"] for message in sent if message["type"] == "http.response.start")
    return status, calls["app"]


async def test_cross_site_post_is_rejected_before_reaching_the_app():
    status, called = await _run(origin="https://evil.example")
    assert status == 403
    assert called is False


async def test_configured_frontend_origin_is_allowed():
    status, called = await _run(origin="https://frontend.example")
    assert (status, called) == (200, True)


async def test_same_host_origin_is_allowed():  # 生产同源部署
    status, called = await _run(origin="http://app.test", host="app.test")
    assert (status, called) == (200, True)


async def test_referer_is_used_when_origin_header_is_absent():
    assert (await _run(referer="https://evil.example/page"))[0] == 403
    assert (await _run(referer="http://app.test/login"))[0] == 200


async def test_missing_headers_are_allowed_for_non_browser_clients():
    assert (await _run())[0] == 200


async def test_sec_fetch_site_cross_site_without_origin_is_rejected():
    assert (await _run(sec_fetch_site="cross-site"))[0] == 403


async def test_null_origin_is_rejected():
    assert (await _run(origin="null"))[0] == 403


async def test_safe_methods_are_not_blocked():
    assert (await _run(origin="https://evil.example", method="GET", path="/api/models"))[0] == 200
    assert (await _run(origin="https://evil.example", method="OPTIONS", path="/api/auth/login"))[0] == 200


async def test_wildcard_origin_config_does_not_allow_cross_site_writes():
    """`*` 只影响 CORS 响应头，不再连带放行写请求。

    登录 CSRF 不靠凭据：跨站顶层表单 POST 照样能写入新会话 cookie，把受害者
    静默登入攻击者账号（M-01），所以这道防线不能因为配了 `*` 就整层消失。
    """
    status, called = await _run(origin="https://anywhere.example", allowed_origins=("*",))
    assert (status, called) == (403, False)


async def test_wildcard_origin_config_still_allows_configured_and_same_host_origins():
    assert (await _run(origin="http://app.test", host="app.test", allowed_origins=("*",)))[0] == 200
    assert (await _run(origin="https://frontend.example", allowed_origins=("*", "https://frontend.example")))[0] == 200


async def test_explicit_allow_any_origin_switch_restores_arbitrary_origins():
    """确实需要任意浏览器的写请求时走显式开关（默认关闭）。"""
    status, called = await _run(origin="https://anywhere.example", allowed_origins=("*",), allow_any_origin=True)
    assert (status, called) == (200, True)


async def test_allow_any_origin_switch_still_rejects_null_origin():
    """`null`（沙箱 iframe / data: 页面）不携带来源信息，即使开了开关也拒绝。"""
    assert (await _run(origin="null", allow_any_origin=True))[0] == 403


async def test_rejection_is_logged_and_counted(caplog):
    """拒绝必须留痕：反代改写 Host / 漏配来源时，生产所有写请求 403 而服务端零线索。"""
    before = metrics.counter_value("csrf_rejected_total", reason="origin_not_allowed")
    with caplog.at_level(logging.WARNING, logger="app.middleware"):
        status, _ = await _run(origin="https://evil.example")

    assert status == 403
    assert metrics.counter_value("csrf_rejected_total", reason="origin_not_allowed") == before + 1
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "https://evil.example" in messages
    assert "/api/auth/login" in messages


async def test_referer_rejection_logs_only_the_normalised_origin(caplog):
    """Referer 的 path/query 可能带令牌或邮箱，日志只能留 scheme://host。"""
    with caplog.at_level(logging.WARNING, logger="app.middleware"):
        status, _ = await _run(referer="https://evil.example/oauth/callback?token=LEAK-CANARY")

    assert status == 403
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "https://evil.example" in messages
    assert "LEAK-CANARY" not in messages


def test_create_app_wires_the_guard_with_cors_origins():
    from app.config import settings

    app = server.create_app()
    guards = [entry for entry in app.user_middleware if entry.cls is OriginGuardMiddleware]
    assert guards, "未注册 OriginGuardMiddleware"
    assert guards[0].kwargs["allowed_origins"] == settings.get_cors_origins()
    assert guards[0].kwargs["allow_any_origin"] is settings.csrf_allow_any_origin
