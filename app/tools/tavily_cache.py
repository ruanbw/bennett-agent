"""Tavily 搜索结果缓存（Redis 共享 + 进程内 LRU 降级）。

## 为什么值得有

搜索是按配额计费的外部调用，而**同一轮对话里重复同一个查询**并不罕见：

- 模型在拿到结果后换个措辞再问一次（「Python 3.13 新特性」→「Python 3.13 有哪些新特性」）；
- 用户重问同一个问题（重新生成、追问「你刚才说的第一条是哪来的」）；
- 同一时刻两个对话在问同一件事。

缓存命中的收益是双份的：省一次配额，也省掉 2~8 秒的等待。

## 边界（刻意保守）

- **只缓存成功结果**：超时、429、5xx 都不进缓存——那些失败往往是瞬时的，缓存它们等于
  把一次抖动固定成十分钟的「搜不到」。
- **TTL 10 分钟**：搜索的价值随时间衰减，新闻类查询尤其如此。宁可再花一次配额，也不要
  把十分钟前的「最新消息」当成现在的事实交给用户。
- **容量 64、LRU 淘汰**（内存后端）：key 是「归一化后的关键词 + max_results」，
  条目再多也只是别的查询，淘汰最久未用的不会影响正确性。
- **Redis 后端的容量由 TTL 控制**：键在 10 分钟后自然消失，不需要额外的清理任务。
  共享 Redis 让多 worker 命中同一份缓存（以前每个进程各一份，命中率按 worker 数衰减）。
- **租户隔离**：键里带用户 id（见 `app/tools/cache_scope.py`），不同用户不共享条目。

## 降级

Redis 不可用时**不报错**：读按 miss 处理、写直接丢弃，并记一条节流告警。缓存是省钱
手段，不是正确性的一部分——把它做成硬依赖等于让一次 Redis 抖动变成「搜索不可用」。
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass

from app.config import settings

#: 缓存条目存活时间（秒）。
SEARCH_CACHE_TTL_SECONDS = 10 * 60.0
#: 最多保留多少条结果。
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

        `scope` 是租户维度（当前实现是用户 id）：不同用户不共享缓存，避免搜索结果里
        的注入内容在他人上下文里存续（I-03）。空串表示不隔离（测试）。
        """
        return (scope, query.strip().lower(), max_results)

    def get(self, query: str, max_results: int, *, scope: str = "", now: float | None = None) -> str | None:
        """取缓存；过期条目顺手删掉并返回 None。"""
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
        """写入结果；超出容量时淘汰最久未用的那一条。"""
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


#: 进程级单例：**降级用**的 LRU（`SEARCH_CACHE_BACKEND=memory` 或 Redis 不可用时）。
#: 测试用 `reset_search_cache()` 隔离。
_memory_cache = SearchResultCache()


class SearchCache:
    """搜索缓存的异步外观：按 `SEARCH_CACHE_BACKEND` 选 Redis 或进程内 LRU。

    ## 为什么不直接叫它 "Redis 缓存"

    这个缓存的作用是「省一次按配额计费的外部调用」，**不是正确性的一部分**。
    所以它必须有明确的降级形态：Redis 挂了就当作 miss，继续搜——绝不能把一次
    缓存故障变成「联网搜索不可用」。

    ## 键的构造

        {prefix}:search:{scope}:{sha256(query|max_results)}

    - `scope` 是租户（用户 id）：不同用户不共享条目，避免搜索结果里的注入内容
      在他人上下文里存续（I-03）；空 scope（测试）用 `-` 占位。
    - query 进哈希而不是直接拼进键：查询里可能有空格、中文、控制字符，直接当键
      既不可读也不好排查，而哈希还顺便统一了长度。
    - 保留 `max_results` 在哈希输入里：5 条与 10 条是两次不同请求。

    ## 与 `SearchResultCache` 的关系

    后者是纯内存实现（sync、可注入时钟、LRU 有界），仍然保留：它是降级形态，
    也是单测里最好断言的一层。本类只负责“选后端 + 序列化 + 隔离”，不重复 LRU 逻辑。
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
        # 写入时明确编码过 UTF-8；解码失败说明键被别人/别的版本写过，按 miss 处理
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
