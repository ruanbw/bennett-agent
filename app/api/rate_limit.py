"""限流的 HTTP 装配层：FastAPI 依赖、429 构造与兼容 re-export。

算法、状态与降级都在 `app/services/rate_limit/`（限流器本体 / 客户端 IP 解析 / 在途流槽位）；
本模块只保留必须贴着 Request/HTTP 生活的部分：依赖函数接收 `Request` 并声明 `Depends`，
`HTTPException` 在这里构造，services 层抛领域异常由这里映射成 429。所有公开符号原样
re-export，既有调用方与测试的 import 路径不变。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from math import ceil

from fastapi import HTTPException, Request, status

from app.config import settings
from app.services.rate_limit import (  # noqa: F401 - 兼容 re-export：既有调用方从本模块 import
    CHAT_MAX_INFLIGHT_STREAMS,
    INFLIGHT_RETRY_AFTER_SECONDS,
    INFLIGHT_STREAM_TTL_SECONDS,
    RATE_LIMIT_WINDOW_SECONDS,
    InFlightStreamLimiter,
    RedisSlidingWindowLimiter,
    SlidingWindowLimiter,
    bucket_ip_address,
    client_key,
    get_inflight_limiter,
    limiter_with,
    make_limiter,
    parse_trusted_proxy_networks,
    resolve_client_ip,
)
from app.services.rate_limit.inflight import InFlightStreamLimitExceeded
from app.services.rate_limit.inflight import inflight_stream_slot as _inflight_stream_slot
from app.services.rate_limit.limiter import make_limiter as _make_limiter

__all__ = [
    "CHAT_MAX_INFLIGHT_STREAMS",
    "INFLIGHT_RETRY_AFTER_SECONDS",
    "INFLIGHT_STREAM_TTL_SECONDS",
    "RATE_LIMIT_WINDOW_SECONDS",
    "InFlightStreamLimitExceeded",
    "InFlightStreamLimiter",
    "RedisSlidingWindowLimiter",
    "SlidingWindowLimiter",
    "bucket_ip_address",
    "client_key",
    "enforce_auth_rate_limit",
    "get_auth_limiter",
    "get_inflight_limiter",
    "get_trusted_proxy_headers",
    "inflight_stream_slot",
    "limiter_with",
    "make_limiter",
    "parse_trusted_proxy_networks",
    "resolve_client_ip",
    "too_many",
]

# ---------------------------------------------------------------------
# 认证端点限流
# ---------------------------------------------------------------------
#: 认证限流桶。与对话限流分开：键的语义（IP vs 账号）与配额都不同。
_auth_limiter: SlidingWindowLimiter | None = None


def too_many(retry_after: float, limit: int | None = None) -> HTTPException:
    """429 响应；`Retry-After` 让客户端知道该等多久，而不是盲目重试。

    `X-RateLimit-*` 需要当前配额，但限流器不是全局单例（配额变化会重建），本函数拿不到它，
    因此 `limit` 由调用方传入，不传就不发这几个头。
    """
    seconds = max(1, ceil(retry_after))
    headers = {"Retry-After": str(seconds)}
    if limit is not None:
        headers["X-RateLimit-Limit"] = str(limit)
        headers["X-RateLimit-Remaining"] = "0"
        # 窗口剩余秒数：与 Retry-After 同一时间基准，客户端不必区分两种语义。
        headers["X-RateLimit-Reset"] = str(seconds)
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=f"请求过于频繁，请在 {seconds} 秒后重试",
        headers=headers,
    )


@asynccontextmanager
async def inflight_stream_slot(user_id: str) -> AsyncIterator[None]:
    """占一条在途对话流的名额；超限抛 429，结束/异常/取消都会归还。

    必须在**返回 SSE 响应之前**进入：响应头一旦发出，超限就只能表现为「200 + 断流」，
    客户端拿不到 429 也读不到原因。services 层抛领域异常，这里映射成 `HTTPException`；
    归还时的取消屏蔽在 services 层的生成器里完成。
    """
    try:
        async with _inflight_stream_slot(user_id):
            yield
    except InFlightStreamLimitExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc


def get_auth_limiter():  # noqa: ANN201
    global _auth_limiter
    _auth_limiter = _make_limiter(_auth_limiter, max(settings.auth_rate_limit_per_minute, 1))
    return _auth_limiter


def get_trusted_proxy_headers() -> list[str]:
    """获取配置中受信的代理标头列表。"""
    raw = settings.trusted_proxy_headers
    if not raw or not raw.strip():
        return []
    return [h.strip() for h in raw.split(",") if h.strip()]


async def enforce_auth_rate_limit(request: Request) -> None:
    """认证端点限流：按 IP 计数（login / register 各自一个桶）。

    挡的是对某账号的密码暴力破解与批量注册：攻击者换账号、目标是常量，所以按 IP 而非账号。
    作用域取 `request.url.path` 的最后一段，同一份依赖挂多个路由上仍能分桶，无需调用方传参。
    """
    if settings.auth_rate_limit_per_minute <= 0:
        return

    direct_host = request.client.host if request.client else None
    trusted_headers = get_trusted_proxy_headers()
    ip = resolve_client_ip(
        direct_host,
        headers=dict(request.headers),
        trusted_headers=trusted_headers,
        trusted_proxies=settings.trusted_proxy_ips,
    )

    scope = request.url.path.rstrip("/").rsplit("/", 1)[-1] or "auth"
    limiter = get_auth_limiter()
    allowed, retry_after = await limiter.acheck(client_key(ip, scope))
    if not allowed:
        raise too_many(retry_after, limit=limiter.limit)
