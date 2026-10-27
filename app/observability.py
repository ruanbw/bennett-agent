"""请求关联 ID 与访问日志。

## 为什么需要它

现在的日志里没有任何能把「一次用户报障」和「一组日志行」连起来的东西：用户说「刚才
那一下失败了」，运维只能按时间去猜。`chat_service._new_error_id` 已经把内部错误的关联
ID 回给前端，但它**只覆盖异常分支**——正常请求、慢请求、429、413 都没有 ID，也就没有
可回查的线索。

这里给每个 HTTP 请求分配一个 ID，并让它同时出现在三个地方：

1. 响应头 `X-Request-ID`（客户端/前端可以记下来，报障时直接给）；
2. 该请求产生的**所有日志行**（通过 `logging.Filter` 注入 `request_id` 字段）；
3. 出错时的 JSON 响应体（`error_id` 与请求 ID 是同一个值）。

## 为什么不引入 OpenTelemetry

没有 trace 后端可接；引入 SDK 只会增加启动依赖与配置面，而拿到的东西（span 树）
在这里没有消费者。一个请求 ID 加结构化日志已经
能回答「这次失败发生在哪一步」，代价是三行代码和一个 ContextVar。

真要接 OTel 时的落点就在这里：`request_id` 作为 trace id 的来源，`RequestContextMiddleware`
是天然的 span 边界。这个模块因此刻意不叫 `tracing`——它不假装自己是分布式追踪。

## 为什么用纯 ASGI 中间件

与 `app.middleware` 里其他几个一致：`BaseHTTPMiddleware` 会给 SSE 额外包一层 task，
而本服务的 `/api/chat` 是长连接流式响应，那层包装有已知的缓冲与取消问题。
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

#: 客户端可以自带 ID（链路里已有网关时不必再生成一个）。限制长度与字符集：
#: 它会进日志与响应头，换行符能伪造日志行，超长值能撑爆日志文件。
REQUEST_ID_HEADER = "X-Request-ID"
MAX_REQUEST_ID_CHARS = 64

#: 请求 ID 允许的字符集。ASCII 字母数字加 `._-`（链路追踪 ID 的常见形态）。
_ALLOWED_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")

#: 当前请求的 ID。`None` 表示不在请求上下文里（CLI、后台任务、单测直调服务层）。
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def current_request_id() -> str | None:
    """当前请求的 ID；不在请求上下文里时为 None。"""
    return _request_id.get()


def new_request_id() -> str:
    """生成一个新的请求 ID（12 位十六进制，与 `chat_service._new_error_id` 同长度）。

    可读性优先于强度：它只用于关联日志，不是安全令牌。16 位十六进制的碰撞概率在
    「一天百万请求」的量级下仍然可以忽略。
    """
    return uuid.uuid4().hex[:12]


def sanitize_request_id(raw: str | None) -> str | None:
    """校验客户端带来的 ID；不合法返回 None（调用方改为自己生成）。

    只允许 `A-Za-z0-9._-`：它会被原样写进日志行与响应头，换行符可以伪造一整条假日志，
    非 ASCII 在各种网关与日志采集器里会变成乱码。长度也有上限，否则一条 1MB 的头就能
    把日志文件撑大。
    """
    if not raw:
        return None
    candidate = raw.strip()
    if not candidate or len(candidate) > MAX_REQUEST_ID_CHARS:
        return None
    # 显式限定 ASCII：`str.isalnum()` 对中文、全角字符也返回 True，用它做校验会让
    # 「带中文」这类值通过，然后在真实网关与日志采集器里变成乱码或被截断。
    if not all(char in _ALLOWED_ID_CHARS for char in candidate):
        return None
    return candidate


class RequestIdFilter(logging.Filter):
    """把当前请求 ID 注入每条日志记录。

    `-` 而不是空串：日志格式里 `%(request_id)s` 空缺时对齐会乱，而 `-` 一眼能看出
    「这条不属于任何请求」（启动日志、后台任务）。

    这个 Filter 挂在**根 handler** 上（`configure_logging`），因此所有 logger（含
    uvicorn 自己的）都会带上这个字段——两条日志流在同一个文件里混排时，这是唯一能把
    它们分开的东西。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = current_request_id() or "-"
        return True


class RequestContextMiddleware:
    """给每个请求分配 ID、写进响应头、并在日志里带上。

    纯 ASGI 实现（理由见模块 docstring）。`finally` 里复位 ContextVar：不复位的话，
    同一个 task 复用（ASGI 服务器会复用 worker task 的一部分上下文）时，下一个请求的
    日志会带着上一个请求的 ID——比没有 ID 更糟，因为它看起来是对的。
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


# ---------------------------------------------------------------------
# HTTP 指标中间件
# ---------------------------------------------------------------------
#: 不算进指标的路径：它们由探测/爬虫高频打，混进 HTTP 指标只会稀释真实信号。
#: 前缀匹配，所以 `/health` 与 `/health/ready` 都算。
_UNMETERED_PREFIXES: tuple[str, ...] = ("/health", "/api/metrics")

#: 路由模板找不到时的兜底标签。
#:
#: 用**模板**而不是实际路径：`/api/x/123` 会让每个 id 变成一个标签组合，直接吃光
#: `MAX_SERIES_PER_METRIC`，然后所有真实指标都变成「被丢弃」（见 metrics 模块）。
_UNMATCHED_ROUTE = "<unmatched>"


def _route_label(scope: dict[str, Any]) -> str:
    """取 FastAPI 解析出的路由模板。

    `scope["route"].path` 只在**路由匹配成功之后**才有，而 ASGI 中间件在匹配之前就
    被调用了——所以这个函数在 `finally` 里被调用，那时 `scope` 已被路由填充。
    """
    route = scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    # 404 等未匹配：用固定标签，不暴露原始路径（它可能含用户数据）
    return _UNMATCHED_ROUTE


class HttpMetricsMiddleware:
    """统计 HTTP 请求数与耗时。

    纯 ASGI（同 `RequestContextMiddleware`）：`BaseHTTPMiddleware` 会给 SSE 额外包一层
    task，对长连接有已知的缓冲/取消问题。

    **流式响应的耗时语义**：这里的计时覆盖到「响应发送完毕」，对 `/api/chat` 就是一整
    条 SSE 流的时长。那正是用户感知的值，但也意味着它远大于普通接口——所以看分位数时
    要按 `route` 分开看，别把所有路由混在一起。
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
            # 记录的时机在 finally：抛异常（客户端断开、内部错误）时也要留下痕迹，
            # 否则「500 变多了」这件事在指标里完全看不见——而它正是最该被看到的。
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
    """把状态码压成分类（`2xx` / `4xx` / …）。

    具体状态码才是有限集合（十几个值），用它当标签也不会有基数问题；但分类对
    「错误率」这件事更直接——看板上要的是 `5xx` 占比，不是 `503` 与 `502` 的对比。

    0（没有响应起始消息，客户端在发响应前就断开）单独记为 `none`：混进 `5xx` 会让
    错误率虚高，而它其实是「用户自己关掉了页面」。
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
