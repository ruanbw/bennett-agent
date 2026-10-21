"""跨站请求守卫（M-01 登录 CSRF 回归锁）。

登录是表单编码的简单请求，SameSite=Lax 不会阻止跨站顶层 POST 写入新 cookie。
`OriginGuardMiddleware` 对 `/api/*` 的非安全方法做 Origin/Referer 校验。
"""

from app import server
from app.middleware import OriginGuardMiddleware


async def _run(origin=None, referer=None, sec_fetch_site=None, method="POST", path="/api/auth/login", host="app.test"):
    calls = {"app": False}
    sent: list[dict] = []

    async def app(scope, receive, send):  # noqa: ANN001
        calls["app"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = OriginGuardMiddleware(app, allowed_origins=["https://frontend.example"])
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


async def test_wildcard_origin_config_allows_any_origin():
    sent: list[dict] = []

    async def app(scope, receive, send):  # noqa: ANN001
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = OriginGuardMiddleware(app, allowed_origins=["*"])
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/auth/login",
        "headers": [(b"host", b"app.test"), (b"origin", b"https://anywhere.example")],
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await middleware(scope, receive, send)
    assert any(message.get("status") == 200 for message in sent)


def test_create_app_wires_the_guard_with_cors_origins():
    from app.config import settings

    app = server.create_app()
    guards = [entry for entry in app.user_middleware if entry.cls is OriginGuardMiddleware]
    assert guards, "未注册 OriginGuardMiddleware"
    assert guards[0].kwargs["allowed_origins"] == settings.get_cors_origins()
