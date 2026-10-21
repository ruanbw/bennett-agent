"""安全响应头与认证响应禁缓存（I-01/I-09）。"""

from app import server
from app.middleware import SecurityHeadersMiddleware


async def _run(path: str, extra_headers: list[tuple[bytes, bytes]] | None = None):
    sent: list[dict] = []

    async def app(scope, receive, send):  # noqa: ANN001
        await send({"type": "http.response.start", "status": 200, "headers": list(extra_headers or [])})
        await send({"type": "http.response.body", "body": b"{}"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    middleware = SecurityHeadersMiddleware(app)
    scope = {"type": "http", "method": "GET", "path": path, "headers": []}
    await middleware(scope, receive, send)

    headers: dict[str, str] = {}
    for message in sent:
        if message.get("type") == "http.response.start":
            headers = {name.decode(): value.decode() for name, value in message.get("headers", [])}
    return headers


async def test_api_responses_get_security_headers_and_no_store():
    headers = await _run("/api/auth/me")
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["cache-control"] == "no-store"


async def test_existing_cache_control_is_not_overwritten():
    headers = await _run("/api/chat", [(b"cache-control", b"no-cache")])
    assert headers["cache-control"] == "no-cache"


async def test_non_api_paths_do_not_get_no_store():
    headers = await _run("/docs")
    assert "cache-control" not in headers
    assert headers["x-content-type-options"] == "nosniff"


def test_create_app_wires_security_headers():
    app = server.create_app()
    assert any(entry.cls is SecurityHeadersMiddleware for entry in app.user_middleware)
