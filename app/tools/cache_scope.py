"""工具缓存的租户作用域。

Tavily 结果缓存是进程级的：key 只有 `(query, max_results)` 时，注入进网页内容的指令会在他人
上下文存续到 TTL 结束（I-03）。这里用 ContextVar 按用户 id 隔离；测试无作用域时为空串。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_SCOPE: ContextVar[str] = ContextVar("tool_cache_scope", default="")


def current_cache_scope() -> str:
    return _SCOPE.get()


@contextmanager
def cache_scope(scope: str | None) -> Iterator[None]:
    token = _SCOPE.set((scope or "").strip())
    try:
        yield
    finally:
        _SCOPE.reset(token)
