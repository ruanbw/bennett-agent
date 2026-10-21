"""Tavily 搜索结果的进程内小缓存。

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
- **容量 64、LRU 淘汰**：key 是「归一化后的关键词 + max_results」，条目再多也只是别的
  查询，淘汰最久未用的不会影响正确性。
- **不跨进程**：多 worker 部署各有各的缓存，这是可接受的——它只是省钱手段，不是正确性
  的一部分。
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass

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
    def make_key(query: str, max_results: int) -> tuple[str, int]:
        """归一化关键词参与 key（大小写与首尾空白不影响命中）。"""
        return (query.strip().lower(), max_results)

    def get(self, query: str, max_results: int, *, now: float | None = None) -> str | None:
        """取缓存；过期条目顺手删掉并返回 None。"""
        key = self.make_key(query, max_results)
        entry = self._entries.get(key)
        if entry is None:
            return None
        current = time.monotonic() if now is None else now
        if entry.expires_at <= current:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return entry.text

    def put(self, query: str, max_results: int, text: str, *, now: float | None = None) -> None:
        """写入结果；超出容量时淘汰最久未用的那一条。"""
        if not text:
            return
        current = time.monotonic() if now is None else now
        key = self.make_key(query, max_results)
        self._entries[key] = _Entry(text=text, expires_at=current + self.ttl_seconds)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        """清空缓存（测试与「想强制拿最新结果」时用）。"""
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


#: 进程级单例：工具函数直接用它，测试用 `reset_search_cache()` 隔离。
search_cache = SearchResultCache()


def reset_search_cache() -> None:
    search_cache.clear()
