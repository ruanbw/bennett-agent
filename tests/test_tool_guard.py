"""工具异常隔离（`ToolErrorIsolation`）的行为锁。

背景：pydantic-ai 默认把工具抛出的非 `ModelRetry` 异常当作本轮失败。工具是外部
边界，一个工具的 bug 不该让整轮回答作废——这里锁住「降级成工具结果、模型继续」
的语义，以及「异常原文只进日志、不进上下文」的外泄边界。
"""

import logging

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import ToolDefinition

from app.services.agent_factory import build_agent
from app.tools.guard import TOOL_FAILURE_RESULT, ToolErrorIsolation


def test_tool_exception_becomes_a_result_instead_of_aborting_the_run(caplog):
    guard = ToolErrorIsolation()
    agent = Agent(TestModel(), capabilities=[guard])

    @agent.tool_plain
    def broken_tool(query: str) -> str:
        """永远失败的工具。"""
        raise RuntimeError("upstream-secret-detail")

    with caplog.at_level(logging.ERROR, logger="app.tools.guard"):
        result = agent.run_sync("你好")

    # 模型看到的是「失败结果」，整轮没有中断
    assert TOOL_FAILURE_RESULT in result.output
    # 异常原文只进日志，不回灌给模型
    assert "upstream-secret-detail" not in result.output
    assert "broken_tool" in caplog.text
    assert "upstream-secret-detail" in caplog.text
    assert "Traceback" in caplog.text


def test_successful_tool_result_is_left_untouched():
    guard = ToolErrorIsolation()
    agent = Agent(TestModel(), capabilities=[guard])

    @agent.tool_plain
    def working_tool(query: str) -> str:
        """正常返回的工具。"""
        return "raw-tool-result"

    result = agent.run_sync("你好")

    assert "raw-tool-result" in result.output
    assert TOOL_FAILURE_RESULT not in result.output


def test_build_agent_installs_tool_error_isolation():
    """生产装配必须挂上隔离能力，否则上面两条行为只存在于测试里。"""
    agent = build_agent("openai/gpt-4o")

    assert any(isinstance(cap, ToolErrorIsolation) for cap in agent.root_capability.capabilities)


# ---------------------------------------------------------------------
# ModelRetry 是控制流，不能被降级
#
# 降级的后果是双重的：模型看不到真实原因（到底哪个参数错了），还收到一句
# 「不要原样重复调用」——对参数校验失败来说正好是反向指导。
# ---------------------------------------------------------------------
def test_model_retry_is_rerouted_to_the_model_not_swallowed():
    """工具抛 `ModelRetry` 时，模型收到的是**它自己的文案**，不是固定失败结果。"""
    guard = ToolErrorIsolation()
    agent = Agent(TestModel(), capabilities=[guard])

    calls = 0

    @agent.tool_plain
    def picky_tool(query: str) -> str:
        """第一次说参数不对，第二次成功。"""
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ModelRetry("query 参数太短，请至少给 3 个字")
        return "ok-result"

    result = agent.run_sync("你好")

    # 走到了第二次调用：重试语义没有被吞（被吞的话 calls 停在 1）
    assert calls == 2
    assert "ok-result" in result.output
    # 模型看到的是原始提示，而不是那句反向指导
    assert TOOL_FAILURE_RESULT not in result.output
    messages = " ".join(str(message) for message in result.all_messages())
    assert "query 参数太短" in messages


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
