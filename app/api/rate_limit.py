"""进程内滑动窗口限流。

本模块的 IP 限流服务于**认证端点**（注册/登录，防密码暴力破解与批量注册）；
对话端点 `/api/chat` 已要求登录，按**账号**限流（见 `app/api/chat.py`），
因为反代部署下 `client.host` 是代理地址，按 IP 计数会退化成全站共享配额。

**本实现是进程内的**：多 worker / 多实例部署时每个进程各自计数，因此它挡的是
「单个来源猛打」，不是精确的全局配额。需要精确配额时应换成 Redis 之类的共享存储。

**不信任客户端可写的 `X-Forwarded-For`**：只有显式配置 `trusted_proxy_headers`
才会读它，且取**最右侧**非空地址——那才是最后一跳代理写入的真实来源；客户端自己
塞进列表最左侧的伪造值被忽略。这要求代理用 `$proxy_add_x_forwarded_for` 之类的
方式**追加或覆盖**该头，而不是原样透传客户端传入的值。

## 两种限流键

本模块提供基元（`SlidingWindowLimiter` / `resolve_client_ip` / `client_key`）与
**认证端点**的限流依赖。对话端点的限流在 `app/api/chat.py`，因为它需要
`current_active_user`（按账号计数），而本模块刻意不依赖任何业务服务。
"""

import logging
import time
from collections import OrderedDict, deque
from math import ceil

from fastapi import HTTPException, Request, status

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

        淘汰顺序按代价从低到高：

        1. 窗口内已经没有命中的键——计数已归零，丢掉不改变任何限流结果；
        2. 未达限的键里最久未使用的那个；
        3. 兜底才丢最久未使用的键，保证内存有界。

        **绝不能把已封禁（已达限）的键排在未达限的键前面**：那等于给攻击者一个后门——
        churn 几个新 key 就能把任意封禁重置掉。
        """
        cutoff = now - self.window_seconds
        evictable_lru: str | None = None
        # 按 LRU 顺序（最旧在前）找候选。过期键直接删掉并 return：迭代器这一步之后再
        # 也不会被推进，所以删除不会触发「遍历中修改」。
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

        `limit <= 0` 表示不限流（与 `settings` 里「0=关闭」的约定一致），直接放行，
        调用方无需自己特判。
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

    def reset(self) -> None:
        """清空计数。供测试使用。"""
        self._hits.clear()


def resolve_client_ip(
    direct_host: str | None,
    headers: dict[str, str] | None = None,
    trusted_headers: list[str] | None = None,
) -> str:
    """从受信任的标头或直连客户端解析真实客户端 IP。

    当配置了 trusted_headers 时，依序从传入的 headers 中取值；对 X-Forwarded-For 这类以逗号
    分隔的列表取**最右侧**非空值。

    取最右而不是最左，是因为最左侧的值来自客户端，取它等于让客户端自选限流键（换一个
    假的 XFF 就换一个桶）。最右侧是最后一跳代理追加的地址——前提是代理**覆盖或追加**
    该头（如 Nginx 的 `$proxy_add_x_forwarded_for`），而不是原样透传。若未配置
    trusted_headers 或未取到值，回退到 direct_host。
    """
    if trusted_headers and headers:
        # headers 可能是 mapping 或 dict，进行大小写不敏感匹配
        lower_headers = {k.lower(): v for k, v in headers.items()}
        for header_name in trusted_headers:
            raw_val = lower_headers.get(header_name.lower().strip())
            if raw_val and raw_val.strip():
                # 逗号分隔时取最后一个非空值（代理追加的那个）
                for candidate in reversed(raw_val.split(",")):
                    rightmost_ip = candidate.strip()
                    if rightmost_ip:
                        return rightmost_ip
    return direct_host or "unknown"


def client_key(host: str | None) -> str:
    """限流键：来源 IP。缺失时归入同一个桶，宁可保守也不要放过。"""
    return host or "unknown"


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
    """429 响应。

    `Retry-After` 让客户端知道该等多久，而不是盲目重试；`X-RateLimit-*` 让客户端与
    监控能读到标准配额信息。`limit` 由调用方传入当前限流器的配额——限流器不是全局
    单例（配额变化时会重建），本函数拿不到它，因此不传就不发这几个头。
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


def get_auth_limiter() -> SlidingWindowLimiter:
    global _auth_limiter
    _auth_limiter = limiter_with(_auth_limiter, max(settings.auth_rate_limit_per_minute, 1))
    return _auth_limiter


def get_trusted_proxy_headers() -> list[str]:
    """获取配置中受信的代理标头列表。"""
    raw = settings.trusted_proxy_headers
    if not raw or not raw.strip():
        return []
    return [h.strip() for h in raw.split(",") if h.strip()]


async def enforce_auth_rate_limit(request: Request) -> None:
    """认证端点限流：按 **IP** 计数。

    挡的是两件事：对某个账号的密码暴力破解，以及批量注册。这两个场景里攻击者
    换的是账号（或来源）、目标是常量，所以必须按 IP 而不是按账号计数。

    在这之前认证端点**完全没有限流**，密码可以被无限次尝试。
    """
    if settings.auth_rate_limit_per_minute <= 0:
        return

    direct_host = request.client.host if request.client else None
    trusted_headers = get_trusted_proxy_headers()
    ip = resolve_client_ip(direct_host, headers=dict(request.headers), trusted_headers=trusted_headers)

    limiter = get_auth_limiter()
    allowed, retry_after = limiter.check(client_key(ip))
    if not allowed:
        raise too_many(retry_after, limit=limiter.limit)
