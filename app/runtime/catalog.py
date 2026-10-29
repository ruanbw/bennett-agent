"""工具元数据的查询入口：运行时只认「名字 → (说明, 风险等级)」，不认注册表与 MCP 的实现。

审批框要的那两行字取自这里，所以查不到时必须降级成空值而不是抛错：配置可能在两轮之间变了，
宁可少一行说明，也不能因此不出审批。
"""

from __future__ import annotations

import logging
from typing import Protocol

from app.runtime.tool_registry import RiskLevel

logger = logging.getLogger(__name__)


class ToolCatalog(Protocol):
    """工具元数据的最小暴露面：执行流（审批）只需要读，不需要装配或调用工具。"""

    def describe(self, name: str) -> tuple[str, RiskLevel | None]:
        """返回工具的说明与风险等级；未知工具返回 `("", None)`。"""
        ...


class DefaultToolCatalog:
    """先查本地注册表、再查已发现的 MCP 工具。"""

    def describe(self, name: str) -> tuple[str, RiskLevel | None]:
        try:
            # 函数内 import：`app.tools` 是装配层，模块级 import 会把沙箱与 Tavily 实现栈拉进每个服务。
            # 取 `get_registry()` 而非包属性 `registry`：那个名字与同名子模块共用，别靠 import 历史。
            from app.tools import get_registry

            tool = get_registry().get(name)
        except Exception:  # noqa: BLE001 - 注册表读取失败不该阻断审批
            logger.warning("读取工具元数据失败 (tool=%s)", name, exc_info=True)
            tool = None

        if tool is not None:
            return (tool.description or "", tool.risk_level)

        # MCP 工具的名字带 `server__` 前缀，来自远端工具定义。函数内 import：MCP 客户端要连远端，
        # 读一次元数据不该把它拉进 import 期（发现由 `app/server.py` 的 lifespan 负责）。
        try:
            from app.tools.mcp_client import get_mcp_registry

            for info in get_mcp_registry().cached_tools():
                if info.qualified_name == name:
                    return (info.description or "", RiskLevel.SAFE_WRITE)
        except Exception:  # noqa: BLE001 - 同上
            logger.debug("读取 MCP 工具元数据失败 (tool=%s)", name, exc_info=True)
        return ("", None)


#: 惰性构造：import 期建 catalog 会把注册表装配提前到任何配置校验之前。
_CATALOG: ToolCatalog | None = None


def get_catalog() -> ToolCatalog:
    """取当前 catalog（首次调用时构造并缓存）。"""
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = DefaultToolCatalog()
    return _CATALOG


def set_catalog_for_tests(catalog: ToolCatalog | None) -> None:
    """覆盖/清空 catalog 缓存。只给测试用。"""
    global _CATALOG
    _CATALOG = catalog


__all__ = [
    "DefaultToolCatalog",
    "ToolCatalog",
    "get_catalog",
    "set_catalog_for_tests",
]
