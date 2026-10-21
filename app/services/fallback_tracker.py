"""收集本轮生成里「主模型 → 备用模型」的切换。

`FallbackModel` 的切换发生在模型内部（`fallback_on` 回调），那个回调拿不到
`RunContext`，也没有机会把结果交给对话流。这里用 `ContextVar` 暴露一个「本轮
收集器」：装配层的回调调用 `record_fallback()`，`chat_service` 在事件循环里把它
翻译成 `FallbackUsed` 事件。

ContextVar 是 task-local 的：并发请求各持一份，互不串扰；CLI 直接跑 Agent 或
标题生成没有开启收集器时，`record_fallback()` 静默忽略。
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class FallbackOccurrence:
    """一次实际发生的模型切换。

    `reason` 是给人和用户看的类别短语（「上游限流」「上游 503」「请求超时」…），
    由 `upstream_errors.describe_fallback_reason` 生成：只说「已切换」不说为什么，
    用户与运维都只能猜。
    """

    primary_model: str
    fallback_model: str
    reason: str = ""


#: 当前任务的本轮收集器；`None` 表示当前上下文不关心切换（没有对话流在跑）。
_collector: ContextVar[list[FallbackOccurrence] | None] = ContextVar("fallback_collector", default=None)


@contextmanager
def collect_fallbacks() -> Iterator[list[FallbackOccurrence]]:
    """在本轮上下文中开始收集切换；退出时复位。

    用 `contextmanager` 而不是手工 `set/reset`：生成器被提前关闭（客户端断开）时
    `finally` 仍会复位，避免 ContextVar 在 task 里留下脏状态。
    """
    occurrences: list[FallbackOccurrence] = []
    token = _collector.set(occurrences)
    try:
        yield occurrences
    finally:
        _collector.reset(token)


def record_fallback(primary_model: str, fallback_model: str, reason: str = "") -> None:
    """记录一次实际发生的切换；没有收集器时静默忽略。"""
    occurrences = _collector.get()
    if occurrences is not None:
        occurrences.append(FallbackOccurrence(primary_model, fallback_model, reason))
