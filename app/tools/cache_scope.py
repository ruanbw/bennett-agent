"""工具缓存的租户作用域。

Tavily 结果缓存是进程级的：如果 key 只有 `(query, max_results)`，两个用户问同一个
问题会共享同一份网页内容。内容本身是公开的，但**注入进去的指令**会在他人上下文里
存续到 TTL 结束（I-03）。

这里用 ContextVar 给一轮对话打上作用域（当前是用户 id）：同一用户的不同会话仍可
命中缓存，不同用户之间隔离。CLI / 测试没有作用域时为空串，保持全局缓存的旧行为。
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
