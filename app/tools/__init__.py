"""统一工具注册中心与导出列表"""
from typing import Any, Callable, List

from app.tools.weather import get_current_weather
from app.tools.calculator import calculate_expression

# 集中导出提供给 Agent/Model 调用的工具函数列表
ALL_TOOLS: List[Callable[..., Any]] = [
    get_current_weather,
    calculate_expression,
]

__all__ = [
    "ALL_TOOLS",
    "get_current_weather",
    "calculate_expression",
]
