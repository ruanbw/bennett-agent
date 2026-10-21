"""请求体上限中间件（H-02 回归锁）。

Starlette/uvicorn 默认不限制 body；同步接口的单条 payload 校验发生在 JSON 解析之后，
因此必须在 ASGI 层先挡住超大请求。直接用 ASGI 调用测试，不引入 HTTP 客户端。
"""

from app import server
from app.config import settings
from app.middleware import MaxBodySizeMiddleware


class _Received(Exception):
    """记录 body 已被读取，用于断言「Content-Length 超限时根本没读 body」。"""


def _scope(content_length: int | None = None) -> dict:
    headers = []
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    return {"type": "http", "method": "POST", "path": "/api/sync/push", "headers": headers}


async def _run(app, scope, messages):  # noqa: ANN001
    sent: list[dict] = []

    async def receive():
        if not messages:
            return {"type": "http.disconnect"}
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


def _status(sent: list[dict]) -> int:
    return next(message["status"] for message in sent if message["type"] == "http.response.start")


async def test_declared_content_length_over_limit_is_rejected_without_reading_body():
    called = {"app": False, "read": False}

    async def inner(scope, receive, send):  # noqa: ANN001
        called["app"] = True
        await receive()
        called["read"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = MaxBodySizeMiddleware(inner, max_bytes=1024)
    sent = await _run(
        app,
        _scope(content_length=2048),
        [{"type": "http.request", "body": b"x" * 2048, "more_body": False}],
    )

    assert _status(sent) == 413
    assert called == {"app": False, "read": False}


async def test_chunked_body_over_limit_aborts_with_413():
    """没有 Content-Length 时也必须边读边限制，而不是读完再说。"""

    async def inner(scope, receive, send):  # noqa: ANN001
        while True:
            message = await receive()
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = MaxBodySizeMiddleware(inner, max_bytes=100)
    sent = await _run(
        app,
        _scope(),
        [
            {"type": "http.request", "body": b"x" * 60, "more_body": True},
            {"type": "http.request", "body": b"x" * 60, "more_body": True},
        ],
    )

    assert _status(sent) == 413


async def test_body_within_limit_reaches_the_application():
    async def inner(scope, receive, send):  # noqa: ANN001
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body})

    app = MaxBodySizeMiddleware(inner, max_bytes=1024)
    sent = await _run(
        app,
        _scope(content_length=5),
        [{"type": "http.request", "body": b"hello", "more_body": False}],
    )

    assert _status(sent) == 200
    assert sent[-1]["body"] == b"hello"


def test_app_is_wired_with_the_configured_limit():
    """create_app 必须真正挂上中间件，且使用 settings 的值。"""
    app = server.create_app()
    middleware = [entry for entry in app.user_middleware if entry.cls is MaxBodySizeMiddleware]
    assert middleware, "未注册 MaxBodySizeMiddleware"
    assert middleware[0].kwargs["max_bytes"] == settings.max_request_body_bytes
