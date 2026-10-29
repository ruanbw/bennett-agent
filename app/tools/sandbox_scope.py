"""沙箱工具的身份作用域：这轮对话该用**谁的、哪台**执行环境。

## 与 `cache_scope` 的关系：同一套机制，不同的用途

两者都是 ContextVar、都由服务端在每轮对话开始前注入、都**绝不**出现在工具参数里。
区别只在于它们回答的问题不同：

- `cache_scope`：缓存命中该按**谁**隔离 —— 一元组 `user_id`；
- `sandbox_scope`：用**谁**的、**哪台**机器 —— 二元组 `(user_id, session_id)`。

## 为什么不合并成一个作用域

1. **语义单一**：本仓库的既有约定是「一个作用域模块只表达一件事」。合并之后，任何只想
   传 `user_id` 的调用方都被迫编造一个 `session_id`，而编造出来的值会静静地变成
   一台「谁也不属于」的机器的地址。
2. **失效条件不同**：缓存作用域在未登录时为空；沙箱作用域在「有用户但没有会话」
   时也必须为空——没有会话就没有机器可寻址（一会话一台机）。
3. **合并不会省掉任何检查**：授权判据始终是 `(user_id, session_id)` 两元组，
   在一个 ContextVar 里放一个复合对象，只是把这条规则藏进了一个更适合放标量的位置。

## 为什么身份不能来自模型参数

工具签名里没有 `sandbox_id`、没有 `user_id`、也没有 `session_id`。模型可以填任何 uuid 与任何会话标识：

- 填别人的 `user_id` → 用自己的会话去驱动别人的机器；
- 填别人的 `session_id` → 读别人 workspace 里的文件；
- 填一个既有的 `lease_id` → 那正是原设计里「换 id 越权」的来源。

把 id 从契约里删掉之后，越权不是「被检查拦住」，而是**在构造上不可达**：模型手里
根本没有可以伪造的句柄。这是本模块存在的全部理由。

## `session_id` 不是身份

它由客户端生成（`sess_<uuid4>`），只回答「这轮对话是哪台会话」，不回答「是谁」。
租约表的每条查询都带 `user_id`，所以拿到别人的 `session_id` 也定位不到别人的机器
——「归属」这一道纵深在 `app/services/sandbox/lease.py` 里，而不是靠这个值本身。
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

    `user_id` 缺失或 `session_id` 为空都视为「没有作用域」，而不是「一个默认作用域」：
    沙箱是「一会话一台机」，没有会话就没有机器可寻址。把空 `session_id` 当成合法值
    会让所有没有会话的调用方挤到同一台机器上，那是最坏的一种共享。
    """
    session = (session_id or "").strip()
    scope = SandboxScope(user_id=user_id, session_id=session) if user_id is not None and session else None
    token = _SCOPE.set(scope)
    try:
        yield
    finally:
        _SCOPE.reset(token)


__all__ = ["SandboxScope", "current_sandbox_scope", "sandbox_scope"]
