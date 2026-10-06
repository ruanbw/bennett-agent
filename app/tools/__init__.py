"""统一工具注册中心与导出列表"""
from typing import Any, Callable, List

from app.tools.tavily_search import tavily_search

# 集中导出提供给 Agent/Model 调用的工具函数列表
ALL_TOOLS: List[Callable[..., Any]] = [
    tavily_search,
]

__all__ = [
    "ALL_TOOLS",
    "tavily_search",
]
