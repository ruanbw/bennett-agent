"""请求关联 ID 与访问日志：每个请求一个 ID，同时出现在响应头、日志行与错误响应体里。

纯 ASGI 中间件：`BaseHTTPMiddleware` 会给 SSE 多包一层 task，长连接有已知的缓冲与取消问题。
不引入 OpenTelemetry（没有 trace 后端）；真要接就从这里起，`request_id` 直接当 trace id。
"""

from __future__ import annotations

import logging
import time
import uuid
from contextvars import ContextVar
from typing import Any

from app.services.metrics import (
    HTTP_REQUEST_DURATION,
    HTTP_REQUESTS_TOTAL,
    metrics,
)

#: 客户端可自带 ID（网关已生成时不必再造）；限长度与字符集，因为换行符能伪造日志行。
REQUEST_ID_HEADER = "X-Request-ID"
MAX_REQUEST_ID_CHARS = 64

#: 请求 ID 允许的字符集：ASCII 字母数字加 `._-`。
_ALLOWED_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")

#: 当前请求的 ID。`None` 表示不在请求上下文里（后台任务、单测直调服务层）。
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def current_request_id() -> str | None:
    """当前请求的 ID；不在请求上下文里时为 None。"""
    return _request_id.get()


def new_request_id() -> str:
    """生成请求 ID（12 位十六进制，与 `chat_service._new_error_id` 同长度）：只用于关联日志，不是安全令牌。"""
    return uuid.uuid4().hex[:12]


def sanitize_request_id(raw: str | None) -> str | None:
    """校验客户端带来的 ID，不合法返回 None（调用方改为自己生成）。

    它会原样写进日志行与响应头：换行符能伪造一整条假日志，非 ASCII 会在采集器里变乱码。
    """
    if not raw:
        return None
    candidate = raw.strip()
    if not candidate or len(candidate) > MAX_REQUEST_ID_CHARS:
        return None
    # 不能改用 `str.isalnum()`：它对中文与全角字符也返回 True，值会在采集器里变乱码。
    if not all(char in _ALLOWED_ID_CHARS for char in candidate):
        return None
    return candidate


class RequestIdFilter(logging.Filter):
    """把当前请求 ID 注入每条日志记录，无请求上下文时填 `-`（一眼看出启动日志/后台任务）。

    Filter 挂在根 handler 上（`configure_logging`），uvicorn 自己的日志也会带这个字段。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = current_request_id() or "-"
        return True


class RequestContextMiddleware:
    """给每个请求分配 ID、写进响应头、并在日志里带上。

    `finally` 里必须复位 ContextVar：不复位会让下一个请求的日志带着上一个的 ID，而它看起来是对的。
    """

    def __init__(self, app: Any) -> None:  # noqa: ANN401 - ASGI app
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:  # noqa: ANN401
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = None
        for name, value in scope.get("headers", []):
            if name.lower() == REQUEST_ID_HEADER.lower().encode():
                incoming = value.decode("latin-1")
                break

        request_id = sanitize_request_id(incoming) or new_request_id()
        token = _request_id.set(request_id)

        async def send_with_header(message: dict[str, Any]) -> None:
            # 只改响应起始消息：body 是流式的（SSE），不能碰。
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((REQUEST_ID_HEADER.lower().encode(), request_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            _request_id.reset(token)


# --- HTTP 指标中间件 ----------------------------------------------------
#: 不算进指标的路径：探测/爬虫高频打，混进来只会稀释真实信号；前缀匹配。
_UNMETERED_PREFIXES: tuple[str, ...] = ("/health", "/api/metrics")

#: 路由模板找不到时的兜底标签；标签必须用模板，实际路径会让每个 id 吃光 `MAX_SERIES_PER_METRIC`。
_UNMATCHED_ROUTE = "<unmatched>"


def _route_label(scope: dict[str, Any]) -> str:
    """取 FastAPI 解析出的路由模板；`scope["route"]` 只在匹配成功后才有，故必须在 finally 里调。"""
    route = scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    # 404 等未匹配：用固定标签，不暴露原始路径（它可能含用户数据）
    return _UNMATCHED_ROUTE


class HttpMetricsMiddleware:
    """统计 HTTP 请求数与耗时。

    计时覆盖到响应发送完毕，`/api/chat` 就是一整条 SSE 流的时长，看分位数必须按 `route` 分开。
    """

    def __init__(self, app: Any) -> None:  # noqa: ANN401 - ASGI app
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:  # noqa: ANN401
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if any(path.startswith(prefix) for prefix in _UNMETERED_PREFIXES):
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "?")
        status_holder: dict[str, int] = {"status": 0}

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message.get("status", 0))
            await send(message)

        started = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            # 记在 finally：异常/客户端断开时也要留痕迹，否则「500 变多了」在指标里完全看不见。
            elapsed = time.perf_counter() - started
            route = _route_label(scope)
            status = status_holder["status"] or 0
            metrics.increment(
                HTTP_REQUESTS_TOTAL,
                method=method,
                route=route,
                status=_status_class(status),
            )
            metrics.observe(HTTP_REQUEST_DURATION, elapsed, method=method, route=route)


def _status_class(status: int) -> str:
    """把状态码压成分类（`2xx`/`4xx`/…）：看板要的是 `5xx` 占比，不是 `503` 与 `502` 的对比。

    0（响应前就断开）单独记为 `none`，混进 `5xx` 会让错误率虚高。
    """
    if status <= 0:
        return "none"
    return f"{status // 100}xx"


__all__ = [
    "HTTP_REQUESTS_TOTAL",
    "MAX_REQUEST_ID_CHARS",
    "REQUEST_ID_HEADER",
    "HttpMetricsMiddleware",
    "RequestContextMiddleware",
    "RequestIdFilter",
    "current_request_id",
    "new_request_id",
    "sanitize_request_id",
]
