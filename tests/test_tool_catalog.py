"""`ToolCatalog`：审批框要的两行字（说明、风险等级）只从这一个协议来。

它是「运行时（审批流）」与「工具实现（注册表 / MCP）」之间唯一的接缝：查不到只能降级成空值，
不能抛错——审批是安全边界，元数据缺失也不能变成「不出审批框」。
"""

import logging
from types import SimpleNamespace

from app.tools import catalog as tool_catalog
from app.tools import mcp_client, registry
from app.tools.registry import RiskLevel


class _FakeCatalog:
    def describe(self, name: str) -> tuple[str, RiskLevel | None]:
        return ("假的说明", RiskLevel.READ_ONLY)


def test_local_registry_is_the_first_source():
    description, risk_level = tool_catalog.DefaultToolCatalog().describe("sandbox_reset")

    assert "重置用户的执行环境" in description
    assert risk_level is RiskLevel.SAFE_WRITE


def test_unknown_tool_degrades_to_empty():
    assert tool_catalog.DefaultToolCatalog().describe("no_such_tool") == ("", None)


def test_registry_failure_is_logged_and_degrades(monkeypatch, caplog):
    """注册表读取抛错时按「查不到」处理：少一行说明，也好过整轮 500。"""

    def boom(_name):
        raise RuntimeError("注册表坏了")

    monkeypatch.setattr(registry, "get", boom)

    with caplog.at_level(logging.WARNING, logger="app.tools.catalog"):
        assert tool_catalog.DefaultToolCatalog().describe("sandbox_reset") == ("", None)

    assert "读取工具元数据失败" in caplog.text


def test_mcp_tools_fall_back_to_safe_write(monkeypatch):
    """MCP 工具不在本地注册表里；它的风险等级只能按最保守的可撤销口径给。"""
    info = SimpleNamespace(qualified_name="demo__echo", description="回声")

    class _McpRegistry:
        def cached_tools(self):
            return [info]

    monkeypatch.setattr(mcp_client, "get_mcp_registry", lambda: _McpRegistry())

    assert tool_catalog.DefaultToolCatalog().describe("demo__echo") == ("回声", RiskLevel.SAFE_WRITE)
    # 已发现但名字对不上：仍是查不到，不能顺手把别的 MCP 工具的元数据借过来
    assert tool_catalog.DefaultToolCatalog().describe("demo__other") == ("", None)


def test_catalog_is_injectable_and_resettable():
    """注入点必须能换也能还原：审批流的测试不该依赖真实注册表。"""
    fake = _FakeCatalog()
    tool_catalog.set_catalog_for_tests(fake)
    try:
        assert tool_catalog.get_catalog() is fake
    finally:
        tool_catalog.set_catalog_for_tests(None)

    assert isinstance(tool_catalog.get_catalog(), tool_catalog.DefaultToolCatalog)
