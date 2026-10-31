"""并发维度的限流：在途对话流槽位（Redis 共享计数 + 进程内降级）。

滑动窗口只能回答「每分钟发了多少次」，而一条 SSE 最长挂 600s：单账号每分钟 30 次全部合法，
却可以让 30 条流同时挂着，占满上游配额与连接池——失败的是别人的请求。所以并发与速率是两个
维度，必须分开限制。用 `INCR`/`DECR` 原子计数而不是「先读后写」：并发到达的请求必须落在
同一个原子计数上，否则 N 个请求会一起读到 `n-1` 全部放行（高并发时正好失效）。

Redis 键是 `{redis_key_prefix}:inflight:{key}`，进程被杀时计数靠 TTL 兜底回收；Redis 不可用
时降级为进程内计数。超限抛领域异常 `InFlightStreamLimitExceeded`，由 api 层映射成 429。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio

from app.config import settings

#: 单账号可同时在途的对话流上限。
#: 限流器只能回答「每分钟发了多少次」，而一条 SSE 最长能挂 600s：单账号每分钟 30 次全部合法，
#: 却可以让 30 条流同时挂着，占满上游配额与连接池——失败的是别人的请求（这正是报错的那个现象）。
#: 并发与速率是两个维度，必须分开限制（同 LiteLLM 的 `max_parallel_requests` 与 RPM 并列）。
#: 3 条足够一人开两个标签页加一部手机；超过这个数基本是脚本或泄漏的 cookie。
CHAT_MAX_INFLIGHT_STREAMS = 3

#: 在途计数的兜底过期时间（秒）。必须大于单轮总时长上界（600s）：进程在流中途被杀时，
#: 计数只能靠这个 TTL 清掉；取小了会把还在跑的一轮当成已结束，上限当场失效。
INFLIGHT_STREAM_TTL_SECONDS = 900

#: 撞上并发上限时建议的等待秒数。对方每条流通常几秒内就结束，不需要等满一个限流窗口。
INFLIGHT_RETRY_AFTER_SECONDS = 5


class InFlightStreamLimiter:
    """按 key 统计「此刻正在进行的流」的数量（Redis 共享计数 + 进程内降级）。

    用 `INCR` 而不是「先读后写」：并发到达的请求必须落在同一个原子计数上，否则 N 个请求会一起
    读到 `n-1` 全部放行（与滑动窗口限流同一个理由），在高并发时正好失效。
    """

    def __init__(self, *, limit: int, ttl_seconds: float = INFLIGHT_STREAM_TTL_SECONDS) -> None:
        if limit < 1:
            raise ValueError("InFlightStreamLimiter 的 limit 必须 >= 1，否则所有流都会被拒绝")
        if ttl_seconds <= 0:
            raise ValueError("InFlightStreamLimiter 的 ttl_seconds 必须 > 0，否则计数永不回收")
        self.limit = limit
        self.ttl_seconds = ttl_seconds
        #: 降级用的进程内计数。Redis 恢复后不会迁移过去（只影响降级那一刻正在跑的流）。
        self._local: dict[str, int] = {}

    def _key(self, key: str) -> str:
        return f"{settings.redis_key_prefix}:inflight:{key}"

    async def acquire(self, key: str) -> bool:
        """占一个槽位；超限返回 `False`（调用方据此回 429）。"""
        from app.services.redis_client import get_redis, warn_throttled

        try:
            client = get_redis()
            count = int(await client.incr(self._key(key)))
            if count > self.limit:
                # 先自增再判断，被拒的那次必须减回去，否则计数只增不减
                await client.decr(self._key(key))
                return False
            # 只为拿到槽位的那次设 TTL：被拒的请求不该延长别人的过期时间
            await client.expire(self._key(key), int(self.ttl_seconds))
            return True
        except Exception:  # noqa: BLE001 - 在途计数不能成为新的故障点，一律降级
            warn_throttled("rate_limit", f"Redis 在途流计数不可用，降级为进程内计数（上限 {self.limit} 条）")
            current = self._local.get(key, 0)
            if current >= self.limit:
                return False
            self._local[key] = current + 1
            return True

    async def release(self, key: str) -> None:
        """归还槽位。重复归还只会把计数减成负数，不会锁死账号（负数在下次获取时自增回来）。"""
        from app.services.redis_client import get_redis, warn_throttled

        try:
            client = get_redis()
            remaining = int(await client.decr(self._key(key)))
            if remaining < 0:
                # 只可能是「归还多于占用」（降级切换/进程重启），删掉让下一次请求从 0 开始
                await client.delete(self._key(key))
            # Redis 正常时也要清掉降级期间留下的进程内计数：`acquire` 只在 Redis 抛错时累加它，
            # 而这个分支原来直接 return，于是「Redis 抖动时占位、恢复后归还」会永久留下一条无法回收的
            # 计数（`_local` 没有 TTL）。攒满 `CHAT_MAX_INFLIGHT_STREAMS` 次之后，该账号下次 Redis
            # 抖动就会拿到一个永远解不掉的 429。
            self._local.pop(key, None)
            return
        except Exception:  # noqa: BLE001
            warn_throttled("rate_limit", "Redis 在途流计数不可用，降级为进程内计数")

        current = self._local.get(key, 0) - 1
        if current > 0:
            self._local[key] = current
        else:
            self._local.pop(key, None)

    async def reset(self) -> None:
        """清空计数（测试与「人工解封」用）。只删自己前缀下的键，不动别人的缓存。"""
        from app.services.redis_client import get_redis

        self._local.clear()
        keys = [key async for key in get_redis().scan_iter(match=f"{settings.redis_key_prefix}:inflight:*", count=500)]
        if keys:
            await get_redis().delete(*keys)


#: 进程级单例：上限是模块常量，不随请求变化，重建只会把在途计数清空。
_inflight_limiter: InFlightStreamLimiter | None = None


def get_inflight_limiter() -> InFlightStreamLimiter:
    """取在途限流器（单例）。"""
    global _inflight_limiter
    if _inflight_limiter is None:
        _inflight_limiter = InFlightStreamLimiter(limit=CHAT_MAX_INFLIGHT_STREAMS)
    return _inflight_limiter


class InFlightStreamLimitExceeded(Exception):
    """并发流槽位已占满。携带 429 响应需要的字段落，由 api 层翻译成 `HTTPException`。"""

    def __init__(self, *, limit: int, retry_after_seconds: int) -> None:
        super().__init__(f"同时进行的对话太多（上限 {limit} 条），请等当前回答结束后再试")
        self.limit = limit
        self.retry_after_seconds = retry_after_seconds


@asynccontextmanager
async def inflight_stream_slot(user_id: str) -> AsyncIterator[None]:
    """占一条在途对话流的名额；超限抛 `InFlightStreamLimitExceeded`，结束/异常/取消都会归还。

    必须在**返回 SSE 响应之前**进入：响应头一旦发出，超限就只能表现为「200 + 断流」，
    客户端拿不到 429 也读不到原因。因此调用方用 `AsyncExitStack` 把退出交给流生成器的
    `finally`，而不是用 `async with` 把 yield 包在里面。
    """
    limiter = get_inflight_limiter()
    slot_key = f"user:{user_id}"
    if not await limiter.acquire(slot_key):
        raise InFlightStreamLimitExceeded(limit=limiter.limit, retry_after_seconds=INFLIGHT_RETRY_AFTER_SECONDS)
    try:
        yield
    finally:
        # 客户端断开时本次任务已被取消，普通 await 会在下一个检查点被再次取消，归还就丢了，
        # 该账号要等满 TTL（900s）才能再开会话。屏蔽取消范围只为把计数还回去。
        with anyio.CancelScope(shield=True):
            await limiter.release(slot_key)
