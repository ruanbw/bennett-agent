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
        #: 被追踪的 key 上限。防止伪造来源把内存吃满——超限时整体清空而不是拒绝新 key，
        #: 因为「拒绝新 key」等于给攻击者一个把正常用户挡在外面的开关。
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}

    def check(self, key: str) -> tuple[bool, float]:
        """记录一次访问并返回 `(是否允许, 建议的 Retry-After 秒数)`。"""
        now = time.monotonic()

        if len(self._hits) >= self.max_keys and key not in self._hits:
            logger.warning("限流键数量达到上限 %s，重置计数表", self.max_keys)
            self._hits.clear()

        hits = self._hits.setdefault(key, deque())
        cutoff = now - self.window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()

        if len(hits) >= self.limit:
            retry_after = max(0.0, hits[0] + self.window_seconds - now)
            return False, retry_after

        hits.append(now)
        return True, 0.0

    def reset(self) -> None:
        """清空计数。供测试使用。"""
        self._hits.clear()


def client_key(host: str | None) -> str:
    """限流键：来源 IP。缺失时归入同一个桶，宁可保守也不要放过。"""
    return host or "unknown"
