"""滑动窗口限流（Redis 共享计数 + 进程内降级）。

认证端点按 IP 计数（反代下 `client.host` 是代理地址，全站共享配额），对话端点按账号，
见 `app/api/chat.py`。本模块不依赖业务服务，也不采信客户端可写的 `X-Forwarded-For`。
"""

import ipaddress
import logging
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from math import ceil

import anyio
from fastapi import HTTPException, Request, status

from app.config import settings

logger = logging.getLogger(__name__)

#: 限流窗口长度（秒）。
RATE_LIMIT_WINDOW_SECONDS = 60.0

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


def _parse_ip(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """把 XFF / client.host 里的单个地址解析成 IP；解析失败返回 None。

    兼容 `[::1]`、`[::1]:443`、`1.2.3.4:5678` 这几种代理常见的写法。
    """
    if not value:
        return None
    candidate = value.strip()
    if candidate.startswith("["):
        closing = candidate.find("]")
        if closing == -1:
            return None
        candidate = candidate[1:closing]
    elif candidate.count(":") == 1 and "." in candidate:
        candidate = candidate.rsplit(":", 1)[0]
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def bucket_ip_address(value: str | None) -> str:
    """把来源地址归一化成限流桶键。

    IPv6 的一个订阅通常是一整个 /64，按完整地址计数就能轮换地址绕过限流（I-07），故归并到
    /64，IPv4-mapped 先解包；解析不了的值原样返回，不能归到 "unknown" 而放大误伤。
    """
    ip = _parse_ip(value)
    if ip is None:
        return value or "unknown"
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def parse_trusted_proxy_networks(raw: str | None) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """解析 `TRUSTED_PROXY_IPS`（逗号分隔的 IP/CIDR）。

    空值返回空列表——空列表意味着不采信任何代理标头（fail closed）。非法条目跳过并告警，
    不静默把整份配置作废。
    """
    networks = []
    for item in (raw or "").split(","):
        cleaned = item.strip()
        if not cleaned:
            continue
        try:
            networks.append(ipaddress.ip_network(cleaned, strict=False))
        except ValueError:
            logger.warning("TRUSTED_PROXY_IPS 里的条目不是合法 IP/CIDR，已忽略: %r", cleaned)
    return networks


_warned_missing_proxy_ips = False


def resolve_client_ip(
    direct_host: str | None,
    headers: dict[str, str] | None = None,
    trusted_headers: list[str] | None = None,
    trusted_proxies: str | None = None,
) -> str:
    """从受信任的标头或直连客户端解析真实客户端 IP。

    直连对端必须落在 `TRUSTED_PROXY_IPS` 里，否则完全不采信标头（L-10：能被直连的后端任人自填
    `X-Forwarded-For` 换桶）。XFF 从右往左跳过受信代理，第一个非代理地址才是真实客户端 IP。
    """
    global _warned_missing_proxy_ips

    if not trusted_headers or not headers:
        return bucket_ip_address(direct_host)

    networks = parse_trusted_proxy_networks(trusted_proxies)
    if not networks:
        if not _warned_missing_proxy_ips:
            _warned_missing_proxy_ips = True
            logger.warning(
                "TRUSTED_PROXY_HEADERS 已配置但 TRUSTED_PROXY_IPS 为空：为避免直连伪造标头，"
                "代理标头不会被采信，限流按 client.host 计数。请在 TRUSTED_PROXY_IPS 里配置代理网段。"
            )
        return bucket_ip_address(direct_host)

    direct_ip = _parse_ip(direct_host)
    if direct_ip is None or not any(direct_ip in network for network in networks):
        # 直连对端不是受信代理：它带来的任何标头都不可信
        return bucket_ip_address(direct_host)

    lower_headers = {key.lower(): value for key, value in headers.items()}
    chain: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for header_name in trusted_headers:
        raw_val = lower_headers.get(header_name.lower().strip())
        if not raw_val or not raw_val.strip():
            continue
        chain = [parsed for item in raw_val.split(",") if (parsed := _parse_ip(item)) is not None]
        break

    if not chain:
        return bucket_ip_address(direct_host)

    for candidate in reversed(chain):
        if not any(candidate in network for network in networks):
            return bucket_ip_address(str(candidate))
    # 整条链都是受信代理（异常配置）：退回最左项，至少不会退化成空串
    return bucket_ip_address(str(chain[0]))


def client_key(host: str | None, scope: str = "") -> str:
    """限流键：`[scope:]来源 IP`；缺失时归入同一个桶，宁可保守也不要放过。

    `scope` 把同一 IP 的不同端点分到不同的桶：共用一桶时，有人在一个 NAT 出口猛试密码打满
    配额，同一出口的正常注册也做不了，而两者要挡的攻击面不同。
    """
    return f"{scope}:{host or 'unknown'}" if scope else (host or "unknown")


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


@asynccontextmanager
async def inflight_stream_slot(user_id: str) -> AsyncIterator[None]:
    """占一条在途对话流的名额；超限抛 429，结束/异常/取消都会归还。

    必须在**返回 SSE 响应之前**进入：响应头一旦发出，超限就只能表现为「200 + 断流」，
    客户端拿不到 429 也读不到原因。因此调用方用 `AsyncExitStack` 把退出交给流生成器的
    `finally`，而不是用 `async with` 把 yield 包在里面。
    """
    limiter = get_inflight_limiter()
    slot_key = f"user:{user_id}"
    if not await limiter.acquire(slot_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"同时进行的对话太多（上限 {limiter.limit} 条），请等当前回答结束后再试",
            headers={"Retry-After": str(INFLIGHT_RETRY_AFTER_SECONDS)},
        )
    try:
        yield
    finally:
        # 客户端断开时本次任务已被取消，普通 await 会在下一个检查点被再次取消，归还就丢了，
        # 该账号要等满 TTL（900s）才能再开会话。屏蔽取消范围只为把计数还回去。
        with anyio.CancelScope(shield=True):
            await limiter.release(slot_key)


# ---------------------------------------------------------------------
# 认证端点限流
# ---------------------------------------------------------------------
#: 认证限流桶。与对话限流分开：键的语义（IP vs 账号）与配额都不同。
_auth_limiter: SlidingWindowLimiter | None = None


def limiter_with(current: SlidingWindowLimiter | None, limit: int) -> SlidingWindowLimiter:
    """取限流器；配额变化时重建。

    惰性创建而非模块级常量，是为了让测试能改 `settings` 后立即生效。
    """
    if current is None or current.limit != limit:
        return SlidingWindowLimiter(limit=limit, window_seconds=RATE_LIMIT_WINDOW_SECONDS)
    return current


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
        # 窗口剩余秒数：与本模块 Retry-After 同一时间基准，客户端不必区分两种语义。
        headers["X-RateLimit-Reset"] = str(seconds)
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=f"请求过于频繁，请在 {seconds} 秒后重试",
        headers=headers,
    )


def get_auth_limiter():  # noqa: ANN201
    global _auth_limiter
    _auth_limiter = make_limiter(_auth_limiter, max(settings.auth_rate_limit_per_minute, 1))
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
