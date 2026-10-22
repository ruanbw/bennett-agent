"""知识库检索的身份作用域。

与 `cache_scope.py` 同一套机制（ContextVar），但用途不同，所以分开：

- `cache_scope` 是**缓存隔离**：防止 A 用户的搜索结果被 B 用户命中；
- 这里是**授权边界**：`search_knowledge_base` 工具必须知道「查谁的库」，而这个值
  绝不能来自模型参数。

为什么不把 `user_id` 做成工具参数：模型可以填任何 uuid。那等于让模型（以及能影响
模型输出的人）遍历整个实例的知识库。工具是外部边界，凡是「身份」这种用户不能自己
声明的输入，都必须由服务端在调用前注入。

CLI 没有用户概念，作用域为空 → 工具明确回答「当前会话没有关联知识库」。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_SCOPE: ContextVar[uuid.UUID | None] = ContextVar("knowledge_scope", default=None)


def current_knowledge_scope() -> uuid.UUID | None:
    """当前一轮对话所属的用户；不在对话轮内时为 None。"""
    return _SCOPE.get()


@contextmanager
def knowledge_scope(user_id: uuid.UUID | None) -> Iterator[None]:
    """给一轮对话打上知识库归属。"""
    token = _SCOPE.set(user_id)
    try:
        yield
    finally:
        _SCOPE.reset(token)


__all__ = ["current_knowledge_scope", "knowledge_scope"]
