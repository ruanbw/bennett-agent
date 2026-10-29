"""把工具的意外异常降级成一条工具结果，让本轮对话继续而不是整轮作废。

`ModelRetry` 是控制流（携带给模型看的整改文案），必须原样上抛；异常原文只进日志，不回灌模型（可能含 URL/密钥）。
边界：`CancelledError` 是 `BaseException`，不经过这里，客户端断开仍按取消语义处理。
"""

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import httpx
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry, ToolFailed
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition

logger = logging.getLogger(__name__)

#: 回灌给模型的失败文案：不让它原样重试——同一 bug 再调一次只会白烧 token，带副作用的工具还会执行两次。
TOOL_FAILURE_RESULT = (
    "工具执行失败（内部错误），本次没有取得结果。"
    "不要原样重复调用同一个工具；请换一种方式继续，或如实告知用户该步骤失败。"
)

#: 同一轮里同一工具失败到这个次数后换更强硬的文案：模型会换个参数再试，而 bug 与参数无关。
ESCALATE_AFTER_FAILURES = 2

#: 网络抖动/超时是暂时性的，不能说「停止调用这个工具」：那会让模型放弃一个还能用的工具。
TRANSIENT_FAILURE_RESULT = (
    "工具暂时失败（网络或上游超时），本次没有取得结果。可以再试一次；"
    "若仍然失败，请换一种方式继续，或如实告知用户该步骤没有完成。"
)


def is_transient_tool_error(error: BaseException) -> bool:
    """这次失败是不是「过一会儿可能就好了」的类型。

    只认网络/超时，其余一概按确定性故障处理：宁可对 bug 强硬，也不要把「参数错了」当网络的锅重试。
    `httpx.TimeoutException` 是 `TransportError` 子类，但两行都要留：有些工具会自己重抛 `TimeoutError`。
    """
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return True
    return isinstance(error, httpx.TransportError)


#: 本轮的工具失败计数（工具名 → 次数）；ContextVar 是 task-local 的，并发请求各持一份。
#: `None` 表示没开收集（单测直接调钩子），此时不升级。
_failure_counts: ContextVar[dict[str, int] | None] = ContextVar("tool_failure_counts", default=None)


@contextmanager
def collect_tool_failures() -> Iterator[dict[str, int]]:
    """在一轮对话的上下文里开始统计工具失败；退出时复位。"""
    counts: dict[str, int] = {}
    token = _failure_counts.set(counts)
    try:
        yield counts
    finally:
        _failure_counts.reset(token)


def record_tool_failure(tool_name: str) -> int:
    """记一次失败并返回该工具**本轮累计**失败次数；没有收集器时返回 1。"""
    counts = _failure_counts.get()
    if counts is None:
        return 1
    counts[tool_name] = counts.get(tool_name, 0) + 1
    return counts[tool_name]


def failure_result_message(tool_name: str, failures: int, *, transient: bool = False) -> str:
    """给模型的失败文案：第一次基础版，累计到 `ESCALATE_AFTER_FAILURES` 次后换成「停止调用」。

    纯函数便于单测；`transient=True` 不升级成「停止调用」，改为「可以再试一次」，同样封顶。
    """
    if transient and failures < ESCALATE_AFTER_FAILURES:
        return TRANSIENT_FAILURE_RESULT
    if failures < ESCALATE_AFTER_FAILURES:
        return TOOL_FAILURE_RESULT
    return (
        f"工具 {tool_name} 本轮已连续失败 {failures} 次（内部错误），继续调用它不会成功。"
        "请立即停止调用这个工具，改用其它方式完成用户的请求；若做不到，就直接向用户说明"
        "这一步没能完成以及原因，不要编造结果。"
    )


class ToolErrorIsolation(AbstractCapability[None]):
    """把工具的意外异常转成「失败结果」，让本轮对话继续。"""

    async def on_tool_execute_error(
        self,
        ctx: RunContext[None],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: Any,  # noqa: ANN401
        error: Exception,
    ) -> Any:  # noqa: ANN401
        # 控制流原样上抛（详见模块 docstring）：这是重试语义的第二道保险。
        if isinstance(error, ModelRetry):
            raise error

        # 参数可能是用户数据（搜索词、提问内容），只记工具名与异常；traceback 必须留全。
        failures = record_tool_failure(call.tool_name)
        # 指标不在这里记：失败会落成 ToolResultEvent，由 `event_translation.translate_event` 打点，重复记会翻倍。
        transient = is_transient_tool_error(error)
        # 网络/超时是预期内的记 WARNING，其它是工具 bug 记 ERROR；两者都要 traceback。
        logger.log(
            logging.WARNING if transient else logging.ERROR,
            "工具执行失败 (tool=%s, tool_call_id=%s, 本轮第 %d 次, transient=%s)",
            call.tool_name,
            call.tool_call_id,
            failures,
            transient,
            exc_info=error,
        )
        # 必须 raise ToolFailed 而不是 return 字符串：字符串会被标成 success，前端就拿不到 failed 信号。
        raise ToolFailed(failure_result_message(call.tool_name, failures, transient=transient))
