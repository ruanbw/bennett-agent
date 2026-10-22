"""Tavily 搜索结果缓存的边界：只该省配额，不该把过时或失败的结果当成事实。"""

import pytest

from app.tools.tavily_cache import SearchResultCache, reset_search_cache, search_cache


def test_hit_returns_the_cached_text():
    cache = SearchResultCache(ttl_seconds=60, max_entries=4)

    cache.put("python 3.13", 5, "结果正文", now=1000.0)

    assert cache.get("python 3.13", 5, now=1001.0) == "结果正文"


def test_key_is_case_and_whitespace_insensitive():
    cache = SearchResultCache(ttl_seconds=60, max_entries=4)
    cache.put("Python 3.13", 5, "结果正文", now=1000.0)

    assert cache.get("  python 3.13 ", 5, now=1000.5) == "结果正文"


def test_different_max_results_do_not_share_entries():
    """5 条与 10 条是两次不同的请求，不能拿条数不同的旧结果顶替。"""
    cache = SearchResultCache(ttl_seconds=60, max_entries=4)
    cache.put("python", 5, "五条", now=1000.0)

    assert cache.get("python", 10, now=1001.0) is None


def test_expired_entry_is_dropped():
    cache = SearchResultCache(ttl_seconds=10, max_entries=4)
    cache.put("news", 5, "旧闻", now=1000.0)

    assert cache.get("news", 5, now=1011.0) is None
    # 过期条目已被删除，不会再占容量
    assert len(cache) == 0


def test_lru_eviction_keeps_the_most_recently_used():
    cache = SearchResultCache(ttl_seconds=60, max_entries=2)
    cache.put("a", 5, "A", now=1000.0)
    cache.put("b", 5, "B", now=1000.1)
    # 命中 a：此后 a 是「最近使用」，淘汰时应丢 b
    assert cache.get("a", 5, now=1000.2) == "A"

    cache.put("c", 5, "C", now=1000.3)

    assert cache.get("a", 5, now=1000.4) == "A"
    assert cache.get("b", 5, now=1000.4) is None
    assert cache.get("c", 5, now=1000.4) == "C"


def test_empty_text_is_not_cached():
    cache = SearchResultCache(ttl_seconds=60, max_entries=4)
    cache.put("q", 5, "", now=1000.0)

    assert len(cache) == 0


@pytest.mark.parametrize("kwargs", [{"max_entries": 0}, {"ttl_seconds": 0}, {"ttl_seconds": -1}])
def test_invalid_construction_is_rejected(kwargs):
    with pytest.raises(ValueError):
        SearchResultCache(**kwargs)


def test_singleton_can_be_reset():
    search_cache.put("q", 5, "内容", now=1.0)
    reset_search_cache()

    assert len(search_cache) == 0


def test_cache_is_isolated_by_scope():
    """不同用户不共享搜索结果，避免注入内容在他人上下文里存续（I-03）。"""
    from app.tools.tavily_cache import SearchResultCache

    cache = SearchResultCache()
    cache.put("query", 5, "result-for-user-a", scope="user-a")

    assert cache.get("query", 5, scope="user-a") == "result-for-user-a"
    assert cache.get("query", 5, scope="user-b") is None
    assert cache.get("query", 5) is None


def test_cache_scope_contextvar_isolates_concurrent_tasks():
    import asyncio

    from app.tools.cache_scope import cache_scope, current_cache_scope

    async def worker(scope: str) -> str:
        with cache_scope(scope):
            await asyncio.sleep(0)
            return current_cache_scope()

    async def main() -> list[str]:
        return await asyncio.gather(worker("a"), worker("b"))

    assert asyncio.run(main()) == ["a", "b"]
