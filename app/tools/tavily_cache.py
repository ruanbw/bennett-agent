"""Tavily 搜索结果缓存（Redis 共享 + 进程内 LRU 降级）。

搜索是按配额计费的外部调用，同一轮里重复查询并不罕见；只缓存成功结果（超时/429/5xx 是瞬时的，缓存它们等于把一次抖动
固定成十分钟搜不到），TTL 10 分钟；Redis 不可用时按 miss 处理、不报错，缓存是省钱手段而非正确性的一部分。
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass

from app.config import settings

SEARCH_CACHE_TTL_SECONDS = 10 * 60.0
SEARCH_CACHE_MAX_ENTRIES = 64


@dataclass(frozen=True, slots=True)
class _Entry:
    text: str
    expires_at: float


class SearchResultCache:
    """按 `(query, max_results)` 缓存搜索结果文本的小 LRU + TTL 缓存。"""

    def __init__(
        self,
        *,
        ttl_seconds: float = SEARCH_CACHE_TTL_SECONDS,
        max_entries: int = SEARCH_CACHE_MAX_ENTRIES,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries 必须 >= 1")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds 必须 > 0")
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        #: OrderedDict 天然就是 LRU 顺序：命中时 move_to_end。
        self._entries: OrderedDict[tuple[str, int], _Entry] = OrderedDict()

    @staticmethod
    def make_key(query: str, max_results: int, scope: str = "") -> tuple[str, str, int]:
        """归一化关键词参与 key（大小写与首尾空白不影响命中）。

        `scope` 是租户维度（用户 id）：不同用户不共享，避免搜索结果里的注入内容在他人上下文里存续。
        """
        return (scope, query.strip().lower(), max_results)

    def get(self, query: str, max_results: int, *, scope: str = "", now: float | None = None) -> str | None:
        key = self.make_key(query, max_results, scope)
        entry = self._entries.get(key)
        if entry is None:
            return None
        current = time.monotonic() if now is None else now
        if entry.expires_at <= current:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return entry.text

    def put(self, query: str, max_results: int, text: str, *, scope: str = "", now: float | None = None) -> None:
        if not text:
            return
        current = time.monotonic() if now is None else now
        key = self.make_key(query, max_results, scope)
        self._entries[key] = _Entry(text=text, expires_at=current + self.ttl_seconds)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        """清空缓存（测试与「想强制拿最新结果」时用）。"""
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


#: 进程级单例：降级用的 LRU（`SEARCH_CACHE_BACKEND=memory` 或 Redis 不可用时）；测试用 `reset_search_cache()` 隔离。
_memory_cache = SearchResultCache()


class SearchCache:
    """搜索缓存的异步外观：按 `SEARCH_CACHE_BACKEND` 选 Redis 或进程内 LRU。

    键是 `{prefix}:search:{scope}:{sha256(query|max_results)}`：`scope` 是租户，避免注入内容跨账号存续
    （空 scope 用 `-` 占位），query 进哈希而不是直接当键，`max_results` 参与哈希因为有 5 条与 10 条两种请求。
    """

    def __init__(self, memory: SearchResultCache | None = None) -> None:
        self._memory = memory or _memory_cache

    @property
    def ttl_seconds(self) -> float:
        return SEARCH_CACHE_TTL_SECONDS

    def _redis_key(self, query: str, max_results: int, scope: str) -> str:
        digest = hashlib.sha256(f"{query.strip().lower()}|{max_results}".encode()).hexdigest()
        return f"{settings.redis_key_prefix}:search:{scope or '-'}:{digest}"

    async def get(self, query: str, max_results: int, *, scope: str = "") -> str | None:
        """取缓存；未命中/已过期/后端不可用都返回 None（调用方当作普通 miss）。"""
        if settings.search_cache_backend == "memory":
            return self._memory.get(query, max_results, scope=scope)

        from app.services.redis_client import get_redis, warn_throttled

        try:
            raw = await get_redis().get(self._redis_key(query, max_results, scope))
        except Exception:  # noqa: BLE001 - 缓存故障不能上升为功能故障
            warn_throttled("search_cache", "搜索缓存（Redis）读取失败，本次按未命中处理。")
            return None
        if not raw:
            return None
        # 写入时按 UTF-8 编码；解码失败说明键被别人/别的版本写过，按 miss 处理
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            warn_throttled("search_cache", "搜索缓存内容无法解码，已当作未命中。")
            return None

    async def put(self, query: str, max_results: int, text: str, *, scope: str = "") -> None:
        """写入结果。空文本不写（「没搜到」不是一个值得缓存的结论）。"""
        if not text:
            return
        if settings.search_cache_backend == "memory":
            self._memory.put(query, max_results, text, scope=scope)
            return

        from app.services.redis_client import get_redis, warn_throttled

        try:
            await get_redis().set(
                self._redis_key(query, max_results, scope),
                text.encode("utf-8"),
                ex=int(self.ttl_seconds),
            )
        except Exception:  # noqa: BLE001 - 写失败只意味着少省一次配额
            warn_throttled("search_cache", "搜索缓存（Redis）写入失败，不影响本次回答。")

    async def clear(self) -> None:
        """清空缓存（测试 / 强制拿最新结果）。

        Redis 侧只删本前缀下的键：`FLUSHDB` 会连带清掉限流计数与会话缓存。
        """
        self._memory.clear()
        if settings.search_cache_backend == "memory":
            return

        from app.services.redis_client import get_redis

        client = get_redis()
        pattern = f"{settings.redis_key_prefix}:search:*"
        keys = [key async for key in client.scan_iter(match=pattern, count=500)]
        if keys:
            await client.delete(*keys)


#: 进程级单例：工具函数直接用它。
search_cache = SearchCache()


async def reset_search_cache() -> None:
    """清空缓存（异步：Redis 后端需要 IO）。"""
    await search_cache.clear()


def reset_search_cache_memory_only() -> None:
    """只清内存层（同步，供不接触 Redis 的单元测试使用）。"""
    _memory_cache.clear()
