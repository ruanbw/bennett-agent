"""收集本轮「主模型 → 备用模型」的切换：`FallbackModel` 的 `fallback_on` 回调拿不到 `RunContext`，
只能通过 `ContextVar` 把结果交给对话流。ContextVar 是 task-local 的，并发请求互不串扰；
没有开启收集器时（CLI / 标题生成）`record_fallback()` 静默忽略。
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from app.services.metrics import FALLBACKS_TOTAL, metrics


@dataclass(frozen=True)
class FallbackOccurrence:
    """一次实际发生的模型切换；`reason` 由 `upstream_errors.describe_fallback_reason` 生成。
    只说「已切换」不说为什么，用户与运维都只能猜。
    """

    primary_model: str
    fallback_model: str
    reason: str = ""


#: 当前任务的本轮收集器；`None` 表示当前上下文不关心切换（没有对话流在跑）。
_collector: ContextVar[list[FallbackOccurrence] | None] = ContextVar("fallback_collector", default=None)


@contextmanager
def collect_fallbacks() -> Iterator[list[FallbackOccurrence]]:
    """在本轮上下文中开始收集切换，退出时复位。
    用 `contextmanager` 而非手工 `set/reset`：生成器被提前关闭（客户端断开）时 `finally` 仍会复位。
    """
    occurrences: list[FallbackOccurrence] = []
    token = _collector.set(occurrences)
    try:
        yield occurrences
    finally:
        _collector.reset(token)


def record_fallback(primary_model: str, fallback_model: str, reason: str = "") -> None:
    """记录一次实际发生的切换（没有收集器时静默忽略），并打一笔指标。
    切换是降级信号：没有指标就回答不了「这个月备用模型用了几次」，也就看不出上游稳不稳。
    """
    occurrences = _collector.get()
    if occurrences is not None:
        occurrences.append(FallbackOccurrence(primary_model, fallback_model, reason))
    # 标签值来自配置里的模型 id（有限集合），不会有基数问题
    metrics.increment(FALLBACKS_TOTAL, **{"from": primary_model, "to": fallback_model})
