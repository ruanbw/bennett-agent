"""长轮询唤醒端口：把「这个用户有新版本了」的信号从写入方传给正在挂起的 pull。

**当前只有单进程实现**（`InMemoryNotifier`）：唤醒表在进程内存里，所以 `uvicorn --workers 2`
以上部署时，A 进程的写入唤不醒 B 进程的等待方——它不报错、也不告警，只会静默等到 long_poll
超时。多 worker 部署必须换成 Redis pub/sub：订阅要在**查版本号之前**完成，否则查完与挂起之间
的写入会丢唤醒。协议形状（`subscribe()` 交出订阅对象、由它 `wait()`）就是为这个实现留的，
**现在不要实现它**。

`notify()` 必须在事务提交之后调用：提交前唤醒会让等待方读到旧版本号，白等一轮。
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Protocol
from weakref import WeakValueDictionary


class ChangeSubscription(Protocol):
    """一次挂起。只能由 `ChangeNotifier.subscribe()` 产出。"""

    async def wait(self, timeout: float) -> None:
        """挂起到有变更或超时。超时是长轮询的正常出口，不抛异常。"""


class ChangeNotifier(Protocol):
    """唤醒能力的最小接口：调用方只认它，不认具体实现。"""

    def notify(self, user_id: uuid.UUID) -> None:
        """通知该用户有新版本。必须在事务提交之后调用。"""

    def subscribe(self, user_id: uuid.UUID) -> contextlib.AbstractAsyncContextManager[ChangeSubscription]:
        """注册等待方，进入上下文时即就绪。

        调用方必须在**查版本号之前**进入这个上下文：反过来的话，落在「查完」与「开始等」
        之间的写入会丢唤醒，等待方只能白等到超时。
        """


class InMemoryNotifier:
    """单进程实现：每用户一个 `asyncio.Event`。

    `WeakValueDictionary`：没有请求在等的时候事件会被回收，不会因为历史用户数量增长而泄漏内存。
    `asyncio.Event` 绑定创建时的 event loop，跨 loop 复用会抛「bound to a different event loop」，
    所以换 loop（测试、进程重启）要整个换掉实例，而不是只清表。
    """

    def __init__(self) -> None:
        self._events: WeakValueDictionary[uuid.UUID, asyncio.Event] = WeakValueDictionary()

    def notify(self, user_id: uuid.UUID) -> None:
        event = self._events.get(user_id)
        if event is not None:
            event.set()

    @contextlib.asynccontextmanager
    async def subscribe(self, user_id: uuid.UUID) -> AsyncIterator[ChangeSubscription]:
        # 进入时就 clear，且必须早于调用方查版本号：否则查完之后到达的唤醒会被清掉。
        event = self._event_for(user_id)
        event.clear()
        yield _EventSubscription(event)

    def _event_for(self, user_id: uuid.UUID) -> asyncio.Event:
        event = self._events.get(user_id)
        if event is None:
            event = asyncio.Event()
            self._events[user_id] = event
        return event


class _EventSubscription:
    def __init__(self, event: asyncio.Event) -> None:
        self._event = event

    async def wait(self, timeout: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._event.wait(), timeout=timeout)


_NOTIFIER: ChangeNotifier | None = None


def get_notifier() -> ChangeNotifier:
    """取当前 notifier（首次调用时构造单进程实现）。"""
    global _NOTIFIER
    if _NOTIFIER is None:
        _NOTIFIER = InMemoryNotifier()
    return _NOTIFIER


def set_notifier_for_tests(notifier: ChangeNotifier | None) -> None:
    """覆盖/清空 notifier。只给测试用：`Event` 绑定创建它的 loop，用例之间必须换新实例。"""
    global _NOTIFIER
    _NOTIFIER = notifier


__all__ = [
    "ChangeNotifier",
    "ChangeSubscription",
    "InMemoryNotifier",
    "get_notifier",
    "set_notifier_for_tests",
]
