"""Redis 连接管理（限流计数 / 搜索缓存 / 会话热缓存共用）。

## 定位：加速与共享，不是权威存储

Redis 在这个项目里承担三件事，**没有一件是数据的唯一副本**：

- 限流计数：跨实例共享配额，丢了只是窗口重置（换来一个可被放大的窗口）；
- 搜索缓存：丢了只是多花一次 Tavily 配额；
- 会话热缓存：数据库才是会话的权威来源，丢了只是回源一次主键查询。

因此**没有任何调用方应该在 Redis 故障时把异常抛给用户**。本模块的信号只有两种：
成功、或返回「不可用」让调用方降级。反过来，降级必须**可观测**——所以每次降级都写
一条带固定标签的 warning 日志并累加 `redis_unavailable_total` 指标。静默降级会让
「限流其实一直没生效」这种问题在事故之后才被发现。

## 为什么连接参数这么保守

`socket_timeout` 默认 1 秒：Redis 只是加速层，让它拖住请求 30 秒等于把「缓存抖动」
升级成「接口超时」。同理不做客户端重试——重试会把 1 秒的等待变成 3 秒，而调用方
本来就有降级路径，重试只是把可用性问题放大。

## 键前缀

所有键都带 `settings.redis_key_prefix`。同一个 Redis 实例被多套部署（开发/预发/生产）
共用时，没有前缀意味着一次 `FLUSHDB` 或键名撞车就能跨环境互相破坏。
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

#: 同类告警的最小间隔（秒）。Redis 挂掉时每个请求都会失败，不做节流的话
#: 日志会被同一个堆栈刷满，把真正有用的上下文挤出去。
_WARN_INTERVAL_SECONDS = 60.0
_last_warn: dict[str, float] = {}


def get_redis() -> Redis:
    """取（或创建）共享客户端。

    注意返回的是**同一份连接池**，不要在使用后 close：Redis 客户端是长生命周期的，
    每个请求建连会让 TLS 握手与连接数都成为瓶颈。进程退出时由 `close_redis()` 统一收尾。
    """
    global _client
    if _client is None:
        _client = Redis.from_url(
            settings.redis_url,
            # bytes 而不是 str：限流脚本与二进制值都按字节处理，统一在这里不做解码，
            # 免得在某些命令上出现「有时候是 str 有时候是 bytes」的分裂。
            decode_responses=False,
            socket_connect_timeout=settings.redis_connect_timeout,
            socket_timeout=settings.redis_socket_timeout,
            # 空闲连接的健康检查（秒）：Redis 侧/中间代理会单方面断开空闲连接，
            # 没有它时第一个拿到死连接的请求会失败一次，看起来像偶发故障。
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

    日志里保留 `reason` 作为稳定标识（"rate_limit" / "session_cache" / "search_cache"），
    便于在日志系统里按子系统统计降级次数；消息本身写给第一次看到它的人。
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

    `asyncio.wait_for` 而不是 `asyncio.timeout`：项目最低支持 Python 3.10
    （`app/api/ops.py` 的数据库探针用的是同一种写法）。
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
