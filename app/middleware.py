"""HTTP 中间件：请求体大小上限。

## 为什么需要它

Starlette / uvicorn **默认不限制请求体大小**。本项目的 JSON body 校验发生在 Pydantic
解析**之后**（例如同步接口的单条 payload 上限是 256 KB），也就是说一个 10MB 的请求
会先被完整读进内存、解析成对象，然后才被告知「这条消息太大了」。注册完全开放，任何
人都能反复发送超大 body，把单进程的内存打满（H-02）。

这里在 ASGI 层做两道检查：

1. `Content-Length` 声明超限 → 不读 body，直接 413；
2. 分块传输（没有 Content-Length）→ 边读边累计，超过上限立即中止并 413。

上限是 `settings.max_request_body_bytes`（默认 16 MB），足够容纳对话历史的 400k
字符与正常的同步批次，同时把「一次请求能占多少内存」变成有界值。反向代理上还应再
配一层更小的值（见 `.env.example`）。
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

    纯 ASGI 实现（不用 `BaseHTTPMiddleware`）：后者会额外包一层 task，且对 SSE 这类
    流式响应有已知的缓冲/取消问题，不适合本服务的 `/api/chat`。
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
                    # 抛到本中间件的 try/except；应用（FastAPI/Starlette）不会把它
                    # 当成正常输入继续处理。
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _BodyTooLarge:
            # 只有在响应尚未开始时才能补 413；已经开始写响应说明 body 是在响应
            # 中途被读的（本服务没有这种端点），此时只能让连接按异常关闭。
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
    """拒绝「浏览器发起的跨站状态变更请求」。

    ## 为什么不能只靠 SameSite=Lax

    登录是**表单编码**的简单请求，攻击页可以自动提交一个跨站 `<form method=POST>`。
    `SameSite=Lax` 只限制「是否携带已有 cookie」，并不阻止跨站顶层 POST 的响应
    **写入新的会话 cookie**——受害者会被静默登入攻击者账号（M-01）。HttpOnly 同样
    无关。

    ## 规则

    对 `/api/*` 的非安全方法（POST/PUT/PATCH/DELETE）：

    1. 有 `Origin`：必须命中 CORS 允许来源，或与请求 `Host` 同源；
    2. 没有 `Origin` 但有 `Referer`：按 Referer 的 origin 做同样判断；
    3. 两者都没有：如果 `Sec-Fetch-Site: cross-site` 则拒绝；否则放行
       （curl / 服务间调用等非浏览器客户端不带这些头）。

    允许来源来自 `CORS_ALLOWED_ORIGINS`；同源判断只看 host:port（反代下 scheme
    可能被改写，按 host 比较更稳）。`*` 视为显式放开任意来源（默认不推荐；
    且 `allow_credentials=true` 时配置层已禁止该组合）。
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
