"""工具异常隔离（`ToolErrorIsolation`）的纯逻辑与装配锁。

行为层用例（真实 Agent + TestModel 跑一轮）已随全量/端到端层移除；这里保留两件事：
生产装配确实挂上了隔离能力，以及钩子对 `ModelRetry` 的早退分支——控制流不能被降级。
"""

import pytest
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition

from app.services.agent_factory import build_agent
from app.tools.guard import ToolErrorIsolation


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
