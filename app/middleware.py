"""HTTP 中间件：请求体大小上限、登录 CSRF 守卫、安全响应头。

Starlette 默认不限制请求体大小，而 body 校验发生在 Pydantic 解析之后，且注册完全开放——
不在这里挡，任何人都能用超大 body 把单进程内存打满（H-02）。
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit


#: 请求体超限时抛出的内部异常；只在本模块内用于中断读取。
class _BodyTooLarge(Exception):
    pass


def _declared_content_length(scope: dict[str, Any]) -> int | None:
    """解析 `Content-Length`；缺失或非法返回 None（非法值交给上层框架处理）。"""
    for name, value in scope.get("headers", []):
        if name.lower() == b"content-length":
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


async def _send_413(send: Any) -> None:
    body = json.dumps({"detail": "请求体过大，已拒绝"}, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class MaxBodySizeMiddleware:
    """限制单个 HTTP 请求体大小；超限返回 413。

    纯 ASGI 实现而非 `BaseHTTPMiddleware`：后者多包一层 task，且对 SSE 这类流式响应有
    已知的缓冲/取消问题，不适合 `/api/chat`。
    """

    def __init__(self, app: Any, *, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes 必须 >= 1")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        declared = _declared_content_length(scope)
        if declared is not None and declared > self.max_bytes:
            await _send_413(send)
            return

        received = 0
        response_started = False

        async def tracked_send(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        async def limited_receive() -> dict[str, Any]:
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # 抛到本中间件的 try/except，应用不会把它当正常输入继续处理。
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _BodyTooLarge:
            # 只有响应尚未开始时才能补 413；已开始写说明 body 是响应中途读的（本服务无此
            # 端点），此时只能让连接按异常关闭。
            if not response_started:
                await _send_413(send)


# ---------------------------------------------------------------------
# 跨站请求守卫（登录 CSRF）
# ---------------------------------------------------------------------

#: 不改变服务端状态的方法，不需要 Origin 校验。
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def _header(scope: dict[str, Any], name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _origin_of(url: str) -> str | None:
    """把 URL 归一化成 `scheme://host[:port]`；非 http(s) 返回 None。"""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return None
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _host_of(url: str) -> str | None:
    origin = _origin_of(url)
    if origin is None:
        return None
    return urlsplit(origin).netloc


def _forbidden() -> tuple[int, bytes]:
    body = json.dumps({"detail": "跨站请求被拒绝"}, ensure_ascii=False).encode("utf-8")
    return 403, body


class OriginGuardMiddleware:
    """拒绝浏览器发起的跨站状态变更请求（`/api/*` 的非安全方法）。

    SameSite=Lax 只限制是否携带已有 cookie，不阻止跨站顶层表单 POST 写入新会话 cookie——受害者会被
    静默登入攻击者账号（M-01）。来源取自 CORS_ALLOWED_ORIGINS；都没有时只拒 cross-site。
    """

    def __init__(self, app: Any, *, allowed_origins: list[str]) -> None:
        self.app = app
        self.allowed_origins = {origin.rstrip("/").lower() for origin in allowed_origins}
        self.allow_any = "*" in self.allowed_origins

    def _allowed(self, origin: str, request_host: str | None) -> bool:
        candidate = origin.rstrip("/").lower()
        if not candidate or candidate == "null":
            return False
        if self.allow_any:
            return True
        if candidate in self.allowed_origins:
            return True
        candidate_host = _host_of(candidate)
        return bool(candidate_host and request_host and candidate_host == request_host.lower())

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if scope.get("method", "GET").upper() in _SAFE_METHODS or not scope.get("path", "").startswith("/api"):
            await self.app(scope, receive, send)
            return

        request_host = _header(scope, b"host")
        origin = _header(scope, b"origin")
        referer = _header(scope, b"referer")
        sec_fetch_site = (_header(scope, b"sec-fetch-site") or "").strip().lower()

        if origin is not None:
            allowed = self._allowed(origin, request_host)
        elif referer is not None:
            referer_origin = _origin_of(referer)
            allowed = bool(referer_origin and self._allowed(referer_origin, request_host))
        else:
            allowed = sec_fetch_site != "cross-site"

        if not allowed:
            status_code, body = _forbidden()
            await send(
                {
                    "type": "http.response.start",
                    "status": status_code,
                    "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)


# ---------------------------------------------------------------------
# 安全响应头 / 缓存策略
# ---------------------------------------------------------------------

#: 所有响应都带的基线头：`nosniff` 阻止浏览器把错误页/上游内容按 HTML 猜类型，
#: `X-Frame-Options` 防点击劫持（`/docs` 这类页面也需要）。
_BASE_SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
)


class SecurityHeadersMiddleware:
    """给 `/api/*` 响应加安全头，且默认 `Cache-Control: no-store`。

    `/api/auth/me`、`/api/admin/users` 的响应含账号与角色信息：没有显式 Cache-Control 时，
    浏览器与中间缓存会把带 cookie 的 GET 留在本地（共享电脑）。已有值不覆盖。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        is_api = scope.get("path", "").startswith("/api")

        async def wrapped_send(message: dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {name.lower() for name, _value in headers}
                for name, value in _BASE_SECURITY_HEADERS:
                    if name not in existing:
                        headers.append((name, value))
                if is_api and b"cache-control" not in existing:
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, wrapped_send)
