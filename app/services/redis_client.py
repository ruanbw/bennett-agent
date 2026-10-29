"""Redis 连接管理（限流计数 / 搜索缓存 / 会话热缓存共用）。

这三样都不是唯一副本：调用方只降级、不把异常抛给用户，但降级必须可观测（warning + `redis_unavailable_total`）。
`socket_timeout` 1 秒、不做客户端重试（重试只是把 1 秒变 3 秒）；键一律带 `redis_key_prefix`，避免跨环境撞车。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from redis.asyncio import Redis

from app.config import settings

logger = logging.getLogger(__name__)

#: 进程内单例。惰性创建：导入本模块不应该产生网络连接（测试、CLI、脚本都会导入）。
_client: Redis | None = None

#: 同类告警的最小间隔（秒）：Redis 挂掉时每个请求都会失败，不节流会把日志刷满。
_WARN_INTERVAL_SECONDS = 60.0
_last_warn: dict[str, float] = {}


def get_redis() -> Redis:
    """取（或创建）共享客户端。

    返回的是同一份连接池，用完不要 close（每个请求建连会让 TLS 握手与连接数成为瓶颈），收尾走 `close_redis()`。
    """
    global _client
    if _client is None:
        _client = Redis.from_url(
            settings.redis_url,
            # bytes 而不是 str：限流脚本与二进制值都按字节处理，不在这里解码，免得有的命令给 str 有的给 bytes。
            decode_responses=False,
            socket_connect_timeout=settings.redis_connect_timeout,
            socket_timeout=settings.redis_socket_timeout,
            # 空闲连接健康检查（秒）：Redis/代理会单方面断开空闲连接，没有它时第一个拿到死连接的请求会失败一次。
            health_check_interval=30,
            max_connections=settings.redis_max_connections,
        )
    return _client


async def close_redis() -> None:
    """关闭连接池。进程退出/lifespan 收尾时调用；可重复调用。"""
    global _client
    if _client is None:
        return
    client, _client = _client, None
    try:
        await client.aclose()
    except Exception:  # noqa: BLE001 - 收尾失败不该遮蔽其他清理动作
        logger.warning("关闭 Redis 连接失败", exc_info=True)


def warn_throttled(reason: str, message: str) -> None:
    """同一 reason 每 60 秒最多记一条 warning，并累加降级指标。

    `reason` 是稳定标识（"rate_limit" / "session_cache" / "search_cache"），供日志系统按子系统统计降级次数。
    """
    from app.services.metrics import metrics

    metrics.increment("redis_unavailable_total", reason=reason)

    now = time.monotonic()
    last = _last_warn.get(reason, 0.0)
    if now - last < _WARN_INTERVAL_SECONDS:
        return
    _last_warn[reason] = now
    logger.warning("%s", message)


async def ping_redis(timeout: float = 1.5) -> tuple[bool, str]:
    """就绪探针用：返回 `(是否可用, 失败原因)`。

    用 `asyncio.wait_for` 而非 `asyncio.timeout`：最低支持 Python 3.10（`app/api/ops.py` 的数据库探针同此）。
    """
    import asyncio

    try:
        await asyncio.wait_for(get_redis().ping(), timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError):
        return False, "timeout"
    except Exception as exc:  # noqa: BLE001 - 任何故障都归成「不可用」
        return False, type(exc).__name__
    return True, ""


async def redis_health_detail(timeout: float = 1.5) -> dict[str, Any]:
    """给 `/health/ready` 用的结构化结果（不抛异常、不泄露连接串）。"""
    ok, reason = await ping_redis(timeout=timeout)
    return {"status": "ok" if ok else "unavailable", **({"reason": reason} if reason else {})}


def reset_state_for_tests() -> None:
    """清掉告警节流状态（测试用；不动连接本身）。"""
    _last_warn.clear()
