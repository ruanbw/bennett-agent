"""速率维度的限流器本体：进程内滑动窗口、Redis 共享窗口、装配工厂。

滑动窗口是「最近 N 秒命中了几个」，判断/写入/过期必须在同一原子操作里，否则并发请求会
一起读到旧计数全部放行（低并发测不出来）。进程内实现供单 worker 与降级用；跨 worker 的
共享配额走 Redis ZSET + Lua，时间源用 Redis 的 `TIME`，多实例才落得到同一窗口边界。

键格式与降级语义是契约：Redis 键是 `{redis_key_prefix}:ratelimit:{key}`，Redis 故障时按
`RATE_LIMIT_ON_REDIS_ERROR` 三选一（local_fallback / fail_closed / fail_open），两者都不能变。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import OrderedDict, deque

from app.config import settings

logger = logging.getLogger(__name__)

#: 限流窗口长度（秒）。
RATE_LIMIT_WINDOW_SECONDS = 60.0


class SlidingWindowLimiter:
    """按 key 统计最近 `window_seconds` 秒内的请求数。"""

    def __init__(self, *, limit: int, window_seconds: float, max_keys: int = 10_000) -> None:
        if max_keys < 1:
            raise ValueError("max_keys 必须 >= 1，否则追踪表没有上限")
        if window_seconds <= 0:
            raise ValueError("window_seconds 必须 > 0，否则窗口无意义")
        self.limit = limit
        self.window_seconds = window_seconds
        #: 被追踪的 key 上限。防止伪造来源把内存吃满。
        self.max_keys = max_keys
        #: OrderedDict 天然就是 LRU 顺序：命中的键用 `move_to_end` 移到尾部。
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def _evict(self, now: float) -> None:
        """容量满时腾出一个位置（调用方已确认待插入的 key 不在表中）。

        淘汰优先级：窗口内已无命中的键 → 未达限的 LRU → 兜底 LRU。绝不能把已达限的 key
        排在未达限的 key 前面，否则 churn 几个新 key 就能把任意封禁重置掉。
        """
        cutoff = now - self.window_seconds
        evictable_lru: str | None = None
        # 按 LRU 顺序（最旧在前）找候选。过期键直接删掉并 return：迭代器此后再不会推进，
        # 所以删除不会触发「遍历中修改」。
        for key, hits in self._hits.items():
            if not hits or hits[-1] <= cutoff:
                del self._hits[key]
                return
            if evictable_lru is None and len(hits) < self.limit:
                evictable_lru = key

        if evictable_lru is not None:
            del self._hits[evictable_lru]
            return

        # 所有键都在窗口内且都已打满：极端场景下的保底，内存必须有界。
        self._hits.popitem(last=False)

    def check(self, key: str) -> tuple[bool, float]:
        """记录一次访问并返回 `(是否允许, 建议的 Retry-After 秒数)`。

        `limit <= 0` 表示不限流（与 settings 「0=关闭」的约定一致），直接放行，调用方无需特判。
        """
        now = time.monotonic()

        if self.limit <= 0:
            return True, 0.0

        if len(self._hits) >= self.max_keys and key not in self._hits:
            self._evict(now)

        hits = self._hits.get(key)
        if hits is None:
            hits = deque()
            self._hits[key] = hits
        else:
            self._hits.move_to_end(key)
        cutoff = now - self.window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()

        if len(hits) >= self.limit:
            retry_after = max(0.0, hits[0] + self.window_seconds - now) if hits else self.window_seconds
            return False, retry_after

        hits.append(now)
        return True, 0.0

    async def acheck(self, key: str) -> tuple[bool, float]:
        """异步入口：内存实现直接复用同步逻辑（调用方统一 await）。"""
        return self.check(key)

    def reset(self) -> None:
        """清空计数。供测试使用。"""
        self._hits.clear()


class RedisSlidingWindowLimiter:
    """基于 Redis 的滑动窗口限流器（跨进程 / 跨实例共享配额）。

    判断/写入/设过期必须在同一个 Lua 脚本里、时间源用 Redis 的 `TIME`：否则并发请求会一起读到
    `count = limit - 1` 全放行（低并发测不出来），多实例也落不到同一窗口边界。
    """

    #: KEYS[1] = 限流键；ARGV = limit, window_ms, member。
    #: 返回 {是否允许(0/1), Retry-After 毫秒}。
    _SCRIPT = """
    local key = KEYS[1]
    local limit = tonumber(ARGV[1])
    local window = tonumber(ARGV[2])
    local member = ARGV[3]

    local t = redis.call('TIME')
    local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
    local cutoff = now - window

    redis.call('ZREMRANGEBYSCORE', key, 0, cutoff)
    local count = redis.call('ZCARD', key)

    if count >= limit then
        local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
        local retry = window
        if oldest[2] then
            retry = tonumber(oldest[2]) + window - now
        end
        if retry < 0 then retry = 0 end
        return {0, retry}
    end

    redis.call('ZADD', key, now, member)
    -- 过期时间与窗口一致：窗口内没有新请求时键自然消失，不需要单独的清理任务
    redis.call('PEXPIRE', key, window)
    return {1, 0}
    """

    def __init__(self, *, limit: int, window_seconds: float) -> None:
        if limit < 1:
            raise ValueError("RedisSlidingWindowLimiter 的 limit 必须 >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds 必须 > 0")
        self.limit = limit
        self.window_seconds = window_seconds
        #: 降级用的进程内计数器。与 Redis 同配额，但它只看得到本进程的请求。
        self._fallback = SlidingWindowLimiter(limit=limit, window_seconds=window_seconds)

    def _key(self, key: str) -> str:
        return f"{settings.redis_key_prefix}:ratelimit:{key}"

    async def acheck(self, key: str) -> tuple[bool, float]:
        from app.services.redis_client import get_redis, warn_throttled

        try:
            result = await get_redis().eval(
                self._SCRIPT,
                1,
                self._key(key),
                self.limit,
                int(self.window_seconds * 1000),
                f"{time.time():.6f}:{uuid.uuid4().hex}",
            )
        except Exception:  # noqa: BLE001 - 任何 Redis 故障都走同一个降级决策
            return self._on_redis_failure(key, warn_throttled)

        # redis-py 对 Lua 的返回是 list[bytes|int]，这里统一成 int
        allowed = int(result[0]) == 1
        retry_ms = int(result[1])
        return allowed, max(0.0, retry_ms / 1000.0)

    def _on_redis_failure(self, key: str, warn_throttled) -> tuple[bool, float]:  # noqa: ANN001
        """按配置的失败模式决定放行还是拒绝。"""
        mode = settings.rate_limit_on_redis_error
        warn_throttled(
            "rate_limit",
            f"Redis 限流不可用，降级模式={mode}。local_fallback 期间配额会乘以实例数；fail_closed 会让请求直接 429。",
        )
        if mode == "fail_open":
            return True, 0.0
        if mode == "fail_closed":
            return False, self.window_seconds
        return self._fallback.check(key)

    async def reset(self) -> None:
        """清空本限流器的计数（测试与「人工解封」用）。

        只删自己前缀下的键：`FLUSHDB` 会连带清掉会话缓存与搜索缓存，而调用方要的是把
        限流窗口归零。
        """
        from app.services.redis_client import get_redis

        self._fallback.reset()
        client = get_redis()
        prefix = f"{settings.redis_key_prefix}:ratelimit:*"
        keys = [key async for key in client.scan_iter(match=prefix, count=500)]
        if keys:
            await client.delete(*keys)


def make_limiter(current: object, limit: int):
    """按 `RATE_LIMIT_BACKEND` 选实现；配置/配额变化时重建。

    返回对象统一提供 `await limiter.acheck(key)`。
    """
    if settings.rate_limit_backend == "redis":
        if isinstance(current, RedisSlidingWindowLimiter) and current.limit == limit:
            return current
        return RedisSlidingWindowLimiter(limit=limit, window_seconds=RATE_LIMIT_WINDOW_SECONDS)
    memory = current if isinstance(current, SlidingWindowLimiter) else None
    return limiter_with(memory, limit)


def limiter_with(current: SlidingWindowLimiter | None, limit: int) -> SlidingWindowLimiter:
    """取限流器；配额变化时重建。

    惰性创建而非模块级常量，是为了让测试能改 `settings` 后立即生效。
    """
    if current is None or current.limit != limit:
        return SlidingWindowLimiter(limit=limit, window_seconds=RATE_LIMIT_WINDOW_SECONDS)
    return current
