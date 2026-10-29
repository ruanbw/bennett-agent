"""工具异常隔离（`ToolErrorIsolation`）的纯逻辑与装配锁。

行为层用例（真实 Agent + TestModel 跑一轮）已随全量/端到端层移除；这里保留两件事：
生产装配确实挂上了隔离能力，以及钩子对 `ModelRetry` 的早退分支——控制流不能被降级。
"""

import pytest
from pydantic_ai.exceptions import ModelRetry, ToolFailed
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition

from app.services.agent_factory import build_agent
from app.tools.guard import (
    ESCALATE_AFTER_FAILURES,
    TOOL_FAILURE_RESULT,
    TRANSIENT_FAILURE_RESULT,
    ToolErrorIsolation,
    collect_tool_failures,
    failure_result_message,
    is_transient_tool_error,
    record_tool_failure,
)


def test_build_agent_installs_tool_error_isolation():
    """生产装配必须挂上隔离能力，否则隔离行为只存在于库里。"""
    agent = build_agent("openai/gpt-4o")

    assert any(isinstance(cap, ToolErrorIsolation) for cap in agent.root_capability.capabilities)


async def test_guard_explicitly_reraises_model_retry():
    """钩子自身的单元锁：即使 pydantic-ai 以后改变了 except 顺序，早退分支仍在。"""
    guard = ToolErrorIsolation()
    retry = ModelRetry("修正参数后重试")

    with pytest.raises(ModelRetry) as excinfo:
        await guard.on_tool_execute_error(
            None,  # 早退分支不用 ctx
            call=ToolCallPart(tool_name="t", args={}),
            tool_def=ToolDefinition(name="t"),
            args={},
            error=retry,
        )

    assert excinfo.value is retry


async def test_guard_turns_unexpected_error_into_tool_failed():
    """普通异常要以 ToolFailed 收尾：pydantic-ai 才会落成 outcome='failed'。"""
    guard = ToolErrorIsolation()

    with pytest.raises(ToolFailed) as excinfo:
        await guard.on_tool_execute_error(
            None,
            call=ToolCallPart(tool_name="t", args={}),
            tool_def=ToolDefinition(name="t"),
            args={},
            error=RuntimeError("boom"),
        )

    assert excinfo.value.message == TOOL_FAILURE_RESULT


# ---------------------------------------------------------------------
# 连续失败升级：第二次起明确告诉模型「别再调这个工具」
# ---------------------------------------------------------------------
def test_first_failure_uses_the_baseline_message():
    assert failure_result_message("tavily_search", 1) == TOOL_FAILURE_RESULT


def test_repeated_failure_escalates_and_names_the_tool():
    """一句「不要原样重复调用」不足以让模型停手：它常换一组参数再试，而 bug 与参数无关。"""
    message = failure_result_message("tavily_search", ESCALATE_AFTER_FAILURES)

    assert "tavily_search" in message
    assert "停止调用这个工具" in message
    assert message != TOOL_FAILURE_RESULT


def test_failure_counter_returns_one_without_a_collector():
    """直接跑 Agent、单测直接调钩子时没有收集器：不做升级，但也不能抛。"""
    assert record_tool_failure("t") == 1


def test_failure_counter_is_per_tool_and_resets_after_the_turn():
    with collect_tool_failures() as counts:
        assert record_tool_failure("a") == 1
        assert record_tool_failure("a") == 2
        assert record_tool_failure("b") == 1
        assert counts == {"a": 2, "b": 1}

    # 离开上下文之后回到「没有收集器」的状态，计数不跨轮泄漏
    assert record_tool_failure("a") == 1


async def test_guard_escalates_after_repeated_failures():
    """第二轮起文案要升级，并带上工具名与次数。"""
    guard = ToolErrorIsolation()

    with collect_tool_failures():
        first = await _capture_guard_failure(guard, "tavily_search")
        second = await _capture_guard_failure(guard, "tavily_search")

    assert first == TOOL_FAILURE_RESULT
    assert "tavily_search" in second
    assert "2 次" in second


async def test_guard_failures_of_different_tools_are_counted_separately():
    guard = ToolErrorIsolation()

    with collect_tool_failures():
        await _capture_guard_failure(guard, "tavily_search")
        other = await _capture_guard_failure(guard, "get_current_time")

    assert other == TOOL_FAILURE_RESULT


async def _capture_guard_failure(guard, tool_name):
    with pytest.raises(ToolFailed) as excinfo:
        await guard.on_tool_execute_error(
            None,
            call=ToolCallPart(tool_name=tool_name, args={}),
            tool_def=ToolDefinition(name=tool_name),
            args={},
            error=RuntimeError("boom"),
        )
    return excinfo.value.message


# ---------------------------------------------------------------------
# 网络/超时不算「工具坏了」：可以再试一次，但不许无限重试
# ---------------------------------------------------------------------
def test_transient_errors_are_recognised():
    """连接失败/读超时/自己包的 TimeoutError 都是暂时性的。"""
    import asyncio

    import httpx

    assert is_transient_tool_error(httpx.ConnectError("refused"))
    assert is_transient_tool_error(httpx.ReadTimeout("slow"))
    assert is_transient_tool_error(asyncio.TimeoutError())
    assert is_transient_tool_error(TimeoutError())


def test_permanent_errors_are_not_transient():
    """参数错误、形状错误、自定义异常：重试多少次都一样，按确定性故障处理。"""
    assert not is_transient_tool_error(ValueError("bad arg"))
    assert not is_transient_tool_error(KeyError("missing"))
    assert not is_transient_tool_error(RuntimeError("boom"))


def test_transient_first_failure_says_retry_not_give_up():
    message = failure_result_message("tavily_search", 1, transient=True)

    assert "再试一次" in message
    assert "不要原样重复调用" not in message
    assert "失败 1 次" not in message


def test_transient_failure_still_escalates_after_the_threshold():
    """允许再试一次，但不是无限重试：达到阈值后仍然要求停止调用这个工具。"""
    message = failure_result_message("tavily_search", ESCALATE_AFTER_FAILURES, transient=True)

    assert "tavily_search" in message
    assert "停止调用" in message


def test_permanent_first_failure_keeps_the_old_wording():
    assert failure_result_message("get_current_time", 1) == TOOL_FAILURE_RESULT


async def test_guard_logs_transient_failures_at_warning(caplog):
    """网络抖动记 WARNING（预期内），工具 bug 记 ERROR——否则线上告警全是上游抖动。"""
    import asyncio
    import logging

    guard = ToolErrorIsolation()

    with caplog.at_level(logging.DEBUG, logger="app.tools.guard"), pytest.raises(ToolFailed) as excinfo:
        await guard.on_tool_execute_error(
            None,
            call=ToolCallPart(tool_name="tavily_search", args={}),
            tool_def=ToolDefinition(name="tavily_search"),
            args={},
            error=asyncio.TimeoutError(),
        )

    assert excinfo.value.message == TRANSIENT_FAILURE_RESULT
    assert [record.levelno for record in caplog.records] == [logging.WARNING]
