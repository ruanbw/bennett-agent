"""工具注册中心：只锁生产装配真正用到的部分。

`build_tools()` → `is_enabled` → 挂进 Agent 是唯一的运行时路径；`ToolRegistry.get()`、
`list_tools()`、`ALL_TOOLS` 目前没有任何调用方（元数据是给将来的 HITL 审批预留的），
为它们写测试等于提前锁定一个尚未定型的契约，因此不测。
"""

import pytest

from app.tools import (
    RiskLevel,
    ToolCategory,
    ToolRegistry,
    clarification_tool,
    tavily_tool,
)


def test_managed_tool_metadata():
    """验证元数据（风险等级、分类、标签、超时）正确注入与存储。"""
    assert clarification_tool.category == ToolCategory.INTERACTION
    assert clarification_tool.risk_level == RiskLevel.INTERACTIVE
    assert "clarification" in clarification_tool.tags
    assert clarification_tool.metadata["category"] == "interaction"
    assert clarification_tool.metadata["risk_level"] == "interactive"

    assert tavily_tool.category == ToolCategory.SEARCH
    assert tavily_tool.risk_level == RiskLevel.READ_ONLY
    assert "search" in tavily_tool.tags
    assert tavily_tool.timeout is not None
    assert tavily_tool.timeout > 0


@pytest.mark.asyncio
async def test_managed_tool_callable_passthrough():
    """验证 ManagedTool 实例可直接作为函数调用（`__call__` 透传给底层实现）。"""
    res = await clarification_tool(
        None,
        question="请选择开发语言？",
        options=["Python", "TypeScript"],
    )
    assert "已向用户发起意图确认提问" in res


def test_registry_enabled_checker_resilience():
    """当 enabled_checker 抛出异常时，安全降级为未启用，不让系统崩溃。

    这是 `ManagedTool.is_enabled` 唯一的可失败分支，而 `is_enabled` 是生产装配
    （`build_tools`）唯一读取的元数据。
    """
    reg = ToolRegistry()

    def buggy_checker():
        raise RuntimeError("Config service unreachable")

    tool = reg.register(enabled_checker=buggy_checker)(lambda: "fail")
    assert not tool.is_enabled
    assert reg.build_tools() == []


def test_registry_build_tools_filters_by_enabled_state():
    """`build_tools` 只挂启用的工具——这是 `agent_factory` 依赖的装配契约。"""
    reg = ToolRegistry()

    @reg.register(category=ToolCategory.SEARCH)
    def always_on() -> str:
        return "on"

    @reg.register(enabled_checker=lambda: False)
    def always_off() -> str:
        return "off"

    assert [tool.name for tool in reg.build_tools()] == ["always_on"]
