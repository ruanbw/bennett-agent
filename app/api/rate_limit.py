"""进程内滑动窗口限流。

C-3：`/api/chat` 允许匿名调用，没有限流时任何能访问该端口的人都在消耗运维的上游
模型额度。默认监听地址已改为回环（`main.py`），但那只挡住了「不是本机的人」，
挡不住本机上一个失控的脚本。

**本实现是进程内的**：多 worker / 多实例部署时每个进程各自计数，因此它挡的是
「单个来源猛打」，不是精确的全局配额。需要精确配额时应换成 Redis 之类的共享存储。

**不信任 `X-Forwarded-For`**：盲目采信会让客户端自选限流键，等于没有限流。反向
代理后部署时应改为信任代理写入的、由代理剥掉外部同名头的那个字段。
"""

import logging
import time
from collections import deque

logger = logging.getLogger(__name__)


class SlidingWindowLimiter:
    """按 key 统计最近 `window_seconds` 秒内的请求数。"""

    def __init__(self, *, limit: int, window_seconds: float, max_keys: int = 10_000) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        #: 被追踪的 key 上限。防止伪造来源把内存吃满。
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}

    def _evict(self, now: float) -> None:
        """清理过期时间戳，若仍然达到上限则淘汰最旧的键（LRU/最久未访问的项）。"""
        cutoff = now - self.window_seconds
        expired_keys = [k for k, timestamps in self._hits.items() if not timestamps or timestamps[-1] <= cutoff]
        for k in expired_keys:
            del self._hits[k]

        if len(self._hits) >= self.max_keys:
            # 找到最近访问时间戳最旧（或最早加入）的键进行淘汰
            oldest_key = min(
                self._hits.keys(),
                key=lambda k: self._hits[k][-1] if self._hits[k] else float("-inf"),
            )
            del self._hits[oldest_key]

    def check(self, key: str) -> tuple[bool, float]:
        """记录一次访问并返回 `(是否允许, 建议的 Retry-After 秒数)`。"""
        now = time.monotonic()

        if self.limit <= 0:
            return False, self.window_seconds

        if len(self._hits) >= self.max_keys and key not in self._hits:
            self._evict(now)

        hits = self._hits.setdefault(key, deque())
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
    分隔的列表取第一个（最左侧客户端真实 IP）。若未配置或未取到，回退到 direct_host。
    """
    if trusted_headers and headers:
        # headers 可能是 mapping 或 dict，进行大小写不敏感匹配
        lower_headers = {k.lower(): v for k, v in headers.items()}
        for header_name in trusted_headers:
            raw_val = lower_headers.get(header_name.lower().strip())
            if raw_val and raw_val.strip():
                # 逗号分隔时取第一个 IP
                first_ip = raw_val.split(",")[0].strip()
                if first_ip:
                    return first_ip
    return direct_host or "unknown"


def client_key(host: str | None) -> str:
    """限流键：来源 IP。缺失时归入同一个桶，宁可保守也不要放过。"""
    return host or "unknown"
