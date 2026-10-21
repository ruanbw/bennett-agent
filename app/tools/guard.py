"""工具执行异常隔离。

pydantic-ai 的默认语义是：工具函数抛出非 `ModelRetry` 异常时，异常会中止整轮生成。
但工具是外部边界（网络、第三方 SDK、上游返回的形状），一个工具里的 bug 不该让用户
这一轮回答直接作废——尤其不该在正文已经流出、其它工具已经成功之后。

这里用 pydantic-ai 的 capability 钩子 `on_tool_execute_error` 把意外异常降级成一条
**工具结果**：模型看到「工具失败」后可以换工具、换参数或如实向用户说明，整轮继续。
异常原文只进日志（可能含上游 URL、密钥上下文），不回灌给模型。

## 哪些异常不归这里管

`ModelRetry` 是 **控制流**，不是错误：它携带给模型看的「你哪里做错了、该怎么改」
文案，由 pydantic-ai 自己的重试计数管理。把它也降级成固定文案会同时造成两件事：

1. 模型再也看不到真实原因（参数校验失败的具体字段、工具超时了多久）；
2. 收到一句「不要原样重复调用同一个工具」——对参数校验失败来说正好是反向指导，
   而正确做法恰恰是按提示改正参数再调一次。

pydantic-ai 的 `on_tool_execute_error` 在实现上**已经**把 `ModelRetry` 排除在外
（`tool_manager.py` 在 `except (ToolFailed, ModelRetry): raise` 分支就上抛了，不会走到
本钩子）。这里的显式早退是**第二道保险**：它是一个只依赖公开 API 的断言，不依赖
某个版本的内部 except 顺序。以后升级 pydantic-ai 时若那个分支被重排，我们有测试
会当场变红，而不是工具重试语义静默消失。

边界：`asyncio.CancelledError` 是 `BaseException`，不会进入本钩子，客户端断开仍按
取消语义处理（见 `app/services/chat_service.py`）。工具自己捕获并返回错误文案的路径
（如 `tavily_search`）也不受影响——这里只兜「漏网抛出来」的那一类。
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

#: 回灌给模型的失败文案。用 `ToolFailed` 交给模型（而不是把异常原文塞进上下文）：
#: 它会被落成一条 `outcome='failed'` 的工具结果，模型据此调整策略，
#: 同时避免把内部异常原文（可能含 URL / 密钥上下文）放进上下文。
#: 「不要原样重复调用」是给模型的约束：同一 bug 再调一次大概率还是失败，
#: 重复调用只会白烧 token，并且让带副作用的工具执行两次。
TOOL_FAILURE_RESULT = (
    "工具执行失败（内部错误），本次没有取得结果。"
    "不要原样重复调用同一个工具；请换一种方式继续，或如实告知用户该步骤失败。"
)

#: 同一轮里同一个工具失败到这个次数后换一条**更强硬的**文案。
#:
#: 一句「不要原样重复调用」不足以让模型停手：它常换一组参数再试，而工具里的 bug 与
#: 参数无关，重试只是白烧 token、把这一轮拖到超时。第二次失败起明确告诉它「这个工具
#: 本轮已经坏了，停止调用」，并把次数写出来，模型才更容易改走「如实告知用户」。
ESCALATE_AFTER_FAILURES = 2

#: 升级文案里那句「继续调用它不会成功」只对**确定性**故障成立。
#: 网络抖动、上游超时、连接被重置都是暂时性的：第二次失败时告诉模型「停止调用这个工具」
#: 会让它白白放弃一个其实还可用的工具。这类失败可以再试一次，但也不能无限重试。
TRANSIENT_FAILURE_RESULT = (
    "工具暂时失败（网络或上游超时），本次没有取得结果。可以再试一次；"
    "若仍然失败，请换一种方式继续，或如实告知用户该步骤没有完成。"
)


def is_transient_tool_error(error: BaseException) -> bool:
    """这次失败是不是「过一会儿可能就好了」的类型。

    只认网络/超时这一类：`httpx.TransportError` 覆盖连接失败、读超时、连接重置，
    `asyncio.TimeoutError` 覆盖自己用 `asyncio.wait_for` 包的调用。其余一概按确定性
    故障处理——宁可对 bug 强硬（停止调用），也不要把「参数错了」当成网络的锅反复重试。

    注意顺序：`httpx.TimeoutException` 是 `TransportError` 的子类，这里只需判后者；
    但 `Tavily` 之类自己 catch 后重新抛 `TimeoutError` 的路径也要认，两行都保留。
    """
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return True
    return isinstance(error, httpx.TransportError)


#: 本轮的工具失败计数（工具名 → 次数）。与 `fallback_tracker` 同一个套路：
#: ContextVar 是 task-local 的，并发请求各持一份；`None` 表示当前上下文没开收集
#: （CLI 直接跑 Agent、单测直接调钩子），此时不做升级，只返回基础文案。
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
    """给模型的失败文案：第一次用基础版，之后升级为「停止调用这个工具」。

    纯函数，便于单测；`tool_name` 会出现在升级文案里，让模型明确知道该放弃哪个工具。

    `transient=True`（网络/超时）**不升级成「停止调用」**：那种故障过一会儿可能就好了，
    让模型因此放弃一个还能用的工具是错的。文案改为「可以再试一次」——上限仍是
    `ESCALATE_AFTER_FAILURES` 次之后转成强硬版，避免无限重试把这一轮拖到超时。
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

        # 参数可能是用户数据（搜索词、提问内容），只记工具名与异常。
        # traceback 必须留全，否则工具 bug 会从此变成一条「静默的失败结果」。
        failures = record_tool_failure(call.tool_name)
        transient = is_transient_tool_error(error)
        # 网络/超时是预期内的（上游抖动），记 WARNING；其它异常是工具本身的 bug，记 ERROR。
        # 两者都要 traceback：没有它，工具 bug 会退化成一条「静默的失败结果」。
        logger.log(
            logging.WARNING if transient else logging.ERROR,
            "工具执行失败 (tool=%s, tool_call_id=%s, 本轮第 %d 次, transient=%s)",
            call.tool_name,
            call.tool_call_id,
            failures,
            transient,
            exc_info=error,
        )
        # 用 ToolFailed 而不是直接 return 字符串：pydantic-ai 会把它落成
        # `ToolReturnPart(outcome='failed')`，模型看到的仍是「失败结果」，而前端能
        # 拿到结构化的 failed 信号——return 普通字符串会被标成 success。
        raise ToolFailed(failure_result_message(call.tool_name, failures, transient=transient))
