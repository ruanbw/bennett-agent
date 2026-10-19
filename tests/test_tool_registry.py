"""基于 PydanticAI 原生 Tool 的统一工具注册中心与元数据治理测试。"""

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import Tool

from app.tools import (
    ALL_TOOLS,
    ManagedTool,
    RiskLevel,
    ToolCategory,
    ToolRegistry,
    clarification_tool,
    registry,
    tavily_tool,
)


def test_managed_tool_inherits_pydantic_ai_tool():
    """ManagedTool 必须是标准的 pydantic_ai.Tool 实例。"""
    assert isinstance(clarification_tool, Tool)
    assert isinstance(tavily_tool, Tool)
    assert isinstance(clarification_tool, ManagedTool)
    assert isinstance(tavily_tool, ManagedTool)


def test_managed_tool_name_and_dunder_name():
    """验证工具名与 __name__ 属性一致，满足向后兼容。"""
    assert clarification_tool.name == "ask_user_clarification"
    assert clarification_tool.__name__ == "ask_user_clarification"
    assert tavily_tool.name == "tavily_search"
    assert tavily_tool.__name__ == "tavily_search"


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
    """验证 ManagedTool 实例可直接作为函数调用。"""
    res = await clarification_tool(
        None,
        question="请选择开发语言？",
        options=["Python", "TypeScript"],
    )
    assert "已向用户发起意图确认提问" in res


def test_registry_registration_and_get():
    """验证 ToolRegistry 的注册与单项查找。"""
    reg = ToolRegistry()

    @reg.register(
        category=ToolCategory.DATA,
        risk_level=RiskLevel.SAFE_WRITE,
        tags=["database", "write"],
        timeout=12.0,
    )
    def dummy_db_tool(table: str) -> str:
        """虚拟数据库写入工具。"""
        return f"written to {table}"

    found = reg.get("dummy_db_tool")
    assert found is not None
    assert found.name == "dummy_db_tool"
    assert found.category == ToolCategory.DATA
    assert found.risk_level == RiskLevel.SAFE_WRITE
    assert found.tags == ["database", "write"]
    assert found.timeout == 12.0
    assert found.description == "虚拟数据库写入工具。"


def test_registry_list_tools_filtering():
    """验证根据分类、风险等级、标签与激活状态的多维度筛选。"""
    reg = ToolRegistry()

    @reg.register(category=ToolCategory.SEARCH, risk_level=RiskLevel.READ_ONLY, tags=["web"])
    def search_a() -> str:
        return "a"

    @reg.register(category=ToolCategory.SEARCH, risk_level=RiskLevel.READ_ONLY, tags=["local"])
    def search_b() -> str:
        return "b"

    @reg.register(
        category=ToolCategory.SYSTEM,
        risk_level=RiskLevel.DANGEROUS,
        tags=["admin"],
        enabled_checker=lambda: False,
    )
    def dangerous_sys() -> str:
        return "c"

    # 按分类过滤
    assert len(reg.list_tools(category=ToolCategory.SEARCH)) == 2
    assert len(reg.list_tools(category=ToolCategory.SYSTEM)) == 1

    # 按风险过滤
    assert len(reg.list_tools(risk_level=RiskLevel.DANGEROUS)) == 1
    assert len(reg.list_tools(risk_level=RiskLevel.READ_ONLY)) == 2

    # 按标签过滤
    assert len(reg.list_tools(tag="web")) == 1
    assert len(reg.list_tools(tag="local")) == 1

    # 仅查询已启用
    assert len(reg.list_tools(only_enabled=True)) == 2
    assert len(reg.list_tools(category=ToolCategory.SYSTEM, only_enabled=True)) == 0


def test_registry_enabled_checker_resilience():
    """当 enabled_checker 抛出异常时，安全降级为未启用，不让系统崩溃。"""
    reg = ToolRegistry()

    def buggy_checker():
        raise RuntimeError("Config service unreachable")

    tool = reg.register(enabled_checker=buggy_checker)(lambda: "fail")
    assert not tool.is_enabled
    assert reg.build_tools() == []


def test_agent_integration_with_managed_tools():
    """验证 ManagedTool 挂载进 PydanticAI 原生 Agent 后的函数工具集生成。"""
    test_tools = registry.build_tools()
    test_agent = Agent(TestModel(), tools=test_tools)

    tool_names = set(test_agent._function_toolset.tools.keys())
    assert "ask_user_clarification" in tool_names
    for tool in test_tools:
        assert tool.name in tool_names


def test_global_all_tools_contains_standard_tools():
    """验证全局 ALL_TOOLS 包含了系统的核心工具。"""
    names = {tool.name for tool in ALL_TOOLS}
    assert "ask_user_clarification" in names
    assert "get_current_time" in names
    assert "tavily_search" in names
