"""沙箱工具的身份作用域：这轮对话该用谁的、哪台执行环境。
身份由服务端注入 ContextVar、绝不出现在工具参数里——模型手里没有可伪造的句柄，越权在构造上不可达。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SandboxScope:
    """一轮对话的执行环境归属。不可变：作用域一旦注入就不该被下游改写。"""

    user_id: uuid.UUID
    session_id: str


_SCOPE: ContextVar[SandboxScope | None] = ContextVar("sandbox_scope", default=None)


def current_sandbox_scope() -> SandboxScope | None:
    """当前一轮对话的执行环境归属；不在对话轮内、或没有会话时为 None。"""
    return _SCOPE.get()


@contextmanager
def sandbox_scope(user_id: uuid.UUID | None, session_id: str | None) -> Iterator[None]:
    """给一轮对话打上执行环境归属。
    `user_id` 缺失或 `session_id` 为空都算「没有作用域」：空 session 会让所有无会话的调用方挤到同一台机器。
    """
    session = (session_id or "").strip()
    scope = SandboxScope(user_id=user_id, session_id=session) if user_id is not None and session else None
    token = _SCOPE.set(scope)
    try:
        yield
    finally:
        _SCOPE.reset(token)


__all__ = ["SandboxScope", "current_sandbox_scope", "sandbox_scope"]
