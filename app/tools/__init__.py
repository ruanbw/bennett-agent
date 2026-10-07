"""统一工具注册中心与导出列表"""
from typing import Any, Callable, List

from app.tools.tavily_search import tavily_search
from app.tools.ask_user_clarification import ask_user_clarification

# 集中导出提供给 Agent/Model 调用的工具函数列表
ALL_TOOLS: List[Callable[..., Any]] = [
    tavily_search,
    ask_user_clarification,
]

__all__ = [
    "ALL_TOOLS",
    "tavily_search",
    "ask_user_clarification",
]

