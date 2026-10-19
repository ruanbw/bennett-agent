"""工具异常隔离（`ToolErrorIsolation`）的行为锁。

背景：pydantic-ai 默认把工具抛出的非 `ModelRetry` 异常当作本轮失败。工具是外部
边界，一个工具的 bug 不该让整轮回答作废——这里锁住「降级成工具结果、模型继续」
的语义，以及「异常原文只进日志、不进上下文」的外泄边界。
"""

import logging

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

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
