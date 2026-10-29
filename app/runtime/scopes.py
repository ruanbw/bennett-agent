"""工具运行时的两个 ContextVar 作用域：缓存的租户归属与沙箱的身份归属。

两者都是「服务端注入、绝不出现在工具参数里」的隐式上下文——模型手里没有可伪造的句柄，
越权与串租户在构造上不可达。合在一个模块是因为职责同源：谁在跑这轮工具。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

# ---------------------------------------------------------------------
# 工具缓存：Tavily 结果缓存是进程级的，key 只有 `(query, max_results)` 时，注入进网页内容的
# 指令会在他人上下文存续到 TTL 结束（I-03）。这里用 ContextVar 按用户 id 隔离；测试无作用域时为空串。
# ---------------------------------------------------------------------
_CACHE_SCOPE: ContextVar[str] = ContextVar("tool_cache_scope", default="")


def current_cache_scope() -> str:
    return _CACHE_SCOPE.get()


@contextmanager
def cache_scope(scope: str | None) -> Iterator[None]:
    token = _CACHE_SCOPE.set((scope or "").strip())
    try:
        yield
    finally:
        _CACHE_SCOPE.reset(token)


# ---------------------------------------------------------------------
# 沙箱：这轮对话该用谁的、哪台执行环境。
# ---------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SandboxScope:
    """一轮对话的执行环境归属。不可变：作用域一旦注入就不该被下游改写。"""

    user_id: uuid.UUID
    session_id: str


_SANDBOX_SCOPE: ContextVar[SandboxScope | None] = ContextVar("sandbox_scope", default=None)


def current_sandbox_scope() -> SandboxScope | None:
    """当前一轮对话的执行环境归属；不在对话轮内、或没有会话时为 None。"""
    return _SANDBOX_SCOPE.get()


@contextmanager
def sandbox_scope(user_id: uuid.UUID | None, session_id: str | None) -> Iterator[None]:
    """给一轮对话打上执行环境归属。
    `user_id` 缺失或 `session_id` 为空都算「没有作用域」：空 session 会让所有无会话的调用方挤到同一台机器。
    """
    session = (session_id or "").strip()
    scope = SandboxScope(user_id=user_id, session_id=session) if user_id is not None and session else None
    token = _SANDBOX_SCOPE.set(scope)
    try:
        yield
    finally:
        _SANDBOX_SCOPE.reset(token)


__all__ = [
    "SandboxScope",
    "cache_scope",
    "current_cache_scope",
    "current_sandbox_scope",
    "sandbox_scope",
]
