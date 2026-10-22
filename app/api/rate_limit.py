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

import asyncio
import ipaddress
import logging
import sqlite3
import time
from collections import OrderedDict, deque
from math import ceil
from pathlib import Path

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

    IPv6 的一个订阅通常是一整个 /64：如果按完整地址计数，轮换地址（隐私扩展）
    就能无限换桶绕过限流（I-07）。这里把 IPv6 归并到 /64；IPv4-mapped 先解包成
    IPv4。无法解析的值原样返回（不能因此把请求归到 "unknown" 而放大误伤）。
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

    空值返回空列表——**空列表意味着不采信任何代理标头**（fail closed）。
    非法条目跳过并告警，不静默把整份配置作废。
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

    ## 为什么需要两层信任

    只配「标头名」而不管「谁发来的」是不安全的：后端一旦能被直连，任意客户端都能
    自己写一个 `X-Forwarded-For`，每换一个值就换一个限流桶（L-10）。因此必须同时
    配置 `TRUSTED_PROXY_IPS`；且直连对端本身必须落在这些网段里，否则**完全不采信**
    标头、退回 `direct_host`。

    ## 从右往左找第一个非代理地址

    解析 `X-Forwarded-For` 时从**最右**开始，跳过属于受信代理网段的地址，返回第一个
    非代理地址。这比「取最右」更准确：两跳代理（CDN → Nginx）时，最右是 Nginx 的
    上游 CDN 地址；继续往左跳过所有受信代理后才是真实客户端。也避免了「取最左」被
    客户端自填值欺骗。

    任何无法解析成 IP 的条目都会被忽略，不会变成可自选的限流键。
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
    """限流键：`[scope:]来源 IP`。缺失时归入同一个桶，宁可保守也不要放过。

    `scope` 把同一 IP 的**不同端点**分到不同的桶里。共用同一个桶会造出一个很难
    解释的失败模式：有人在一个 NAT 出口后面猛试密码，把 10 次/分钟打满之后，
    同一出口的**正常注册**也做不了（反之亦然）。两者要挡的攻击面不一样，配额
    不该互相挤占。
    """
    return f"{scope}:{host or 'unknown'}" if scope else (host or "unknown")


class SharedSqliteLimiter:
    """基于 SQLite 文件的滑动窗口限流器（跨进程/worker 共享）。

    进程内限流在多 worker 下配额会被乘以 worker 数（I-07）。这里把「窗口内的命中
    时间戳」放进一个独立的 SQLite 文件：连接每次 check 打开、`BEGIN IMMEDIATE` 下
    完成「清理过期 → 计数 → 插入」，SQLite 的写锁保证并发进程不会同时越过上限。

    文件很小（一个窗口内的请求数），不参与业务库的迁移；路径默认与业务库同目录。
    没有可写文件路径时（内存库、测试）调用方会退回 `SlidingWindowLimiter`。
    """

    def __init__(self, *, limit: int, window_seconds: float, path: str) -> None:
        if limit < 1:
            raise ValueError("SharedSqliteLimiter 的 limit 必须 >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds 必须 > 0")
        self.limit = limit
        self.window_seconds = window_seconds
        self.path = path

    @staticmethod
    def _connect(path: str) -> sqlite3.Connection:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=10.0)
        connection.execute("CREATE TABLE IF NOT EXISTS rate_limit_hits (key TEXT NOT NULL, ts REAL NOT NULL)")
        connection.execute("CREATE INDEX IF NOT EXISTS ix_rate_limit_ts ON rate_limit_hits(ts)")
        return connection

    def check(self, key: str) -> tuple[bool, float]:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        connection = self._connect(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM rate_limit_hits WHERE ts <= ?", (cutoff,))
            row = connection.execute("SELECT COUNT(*), MIN(ts) FROM rate_limit_hits WHERE key = ?", (key,)).fetchone()
            count = int(row[0] or 0)
            if count >= self.limit:
                oldest = float(row[1]) if row[1] is not None else now
                connection.commit()
                return False, max(0.0, oldest + self.window_seconds - now)
            connection.execute("INSERT INTO rate_limit_hits (key, ts) VALUES (?, ?)", (key, now))
            connection.commit()
            return True, 0.0
        finally:
            connection.close()

    async def acheck(self, key: str) -> tuple[bool, float]:
        """在 worker 线程执行同步 SQLite 操作，避免阻塞事件循环。"""
        return await asyncio.to_thread(self.check, key)

    def reset(self) -> None:
        connection = self._connect(self.path)
        try:
            connection.execute("DELETE FROM rate_limit_hits")
            connection.commit()
        finally:
            connection.close()


def _rate_limit_db_path() -> str:
    """限流计数文件路径；无法确定时返回空串（退回内存）。"""
    configured = (settings.rate_limit_db_path or "").strip()
    if configured:
        return configured
    from app.db import engine  # 延迟导入：避免循环（db 导入 config）

    database = engine.url.database
    if not database or database == ":memory:":
        return ""
    return str(Path(database).with_name("rate-limit.db"))


def make_limiter(current: object, limit: int):
    """按 `RATE_LIMIT_BACKEND` 选实现；配置/路径变化时重建。

    返回对象统一提供 `await limiter.acheck(key)`。
    """
    if settings.rate_limit_backend == "sqlite":
        path = _rate_limit_db_path()
        if path:
            if isinstance(current, SharedSqliteLimiter) and current.limit == limit and current.path == path:
                return current
            return SharedSqliteLimiter(limit=limit, window_seconds=RATE_LIMIT_WINDOW_SECONDS, path=path)
    memory = current if isinstance(current, SlidingWindowLimiter) else None
    return limiter_with(memory, limit)


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
    """认证端点限流：按 **IP** 计数（login / register 各自一个桶）。

    挡的是两件事：对某个账号的密码暴力破解，以及批量注册。这两个场景里攻击者
    换的是账号（或来源）、目标是常量，所以必须按 IP 而不是按账号计数。

    在这之前认证端点**完全没有限流**，密码可以被无限次尝试。

    端点作用域从 `request.url.path` 的最后一段取（`/api/auth/login` → `login`），
    这样同一份依赖挂在两个路由上仍然分桶，不需要调用方传参。
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
