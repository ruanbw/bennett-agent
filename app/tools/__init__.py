"""统一工具注册中心与导出列表"""

from collections.abc import Callable
from typing import Any

from app.config import settings
from app.tools.ask_user_clarification import ask_user_clarification
from app.tools.tavily_search import tavily_search

#: **全量**工具列表（含当前配置下不可用的工具）。生产装配请用 `build_tools()`：
#: 它会按配置裁剪。保留本列表是因为「项目里一共有哪些工具」需要一个静态答案。
ALL_TOOLS: list[Callable[..., Any]] = [
    tavily_search,
    ask_user_clarification,
]


def build_tools() -> list[Callable[..., Any]]:
    """按当前配置装配要挂给 Agent 的工具列表。

    - `ask_user_clarification` 始终可用：它是交互协议的一部分（服务端会把它翻译成
      `clarification` 事件），不依赖任何外部服务；
    - `tavily_search` 只在配置了 `TAVILY_API_KEY` 时才挂：没有 key 时这个工具只会
      给模型返回「未配置」错误，白占一轮工具调用的 token，还会让模型以为自己能联网。

    每次调用都重新判断配置，所以调用方必须把结果纳入自己的缓存键——见
    `app/services/agent_factory.py` 的缓存键。
    """
    tools: list[Callable[..., Any]] = [ask_user_clarification]
    if settings.tavily_api_key:
        tools.append(tavily_search)
    return tools


__all__ = [
    "ALL_TOOLS",
    "build_tools",
    "tavily_search",
    "ask_user_clarification",
]
