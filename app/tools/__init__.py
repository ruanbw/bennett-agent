"""统一工具注册中心与导出列表。

基于 PydanticAI 原生 Tool 体系进行扩展与治理，提供集中编目、元数据管理
（分类、风险等级、标签）、超时配置以及按环境动态裁剪装配能力。
"""

from app.config import settings
from app.tools.ask_user_clarification import ask_user_clarification as _raw_ask_clarification
from app.tools.guard import TOOL_FAILURE_RESULT, ToolErrorIsolation
from app.tools.registry import (
    ManagedTool,
    RiskLevel,
    ToolCategory,
    ToolRegistry,
)
from app.tools.tavily_search import (
    TAVILY_TIMEOUT_SECONDS,
)
from app.tools.tavily_search import (
    tavily_search as _raw_tavily_search,
)

#: 全局工具注册中心单例
registry = ToolRegistry()

# ---------------------------------------------------------------------
# 工具集中注册与元数据编目
# ---------------------------------------------------------------------

clarification_tool: ManagedTool = registry.register(
    category=ToolCategory.INTERACTION,
    risk_level=RiskLevel.INTERACTIVE,
    tags=["interaction", "clarification"],
    description=(
        "当用户意图模糊、缺少必要关键信息、存在多种理解路径或分支选择时，向用户发起澄清提问。"
        "前端将以交互弹窗/选项卡片的形式展示给用户，供用户快速点击选择答案或手动输入自定义内容。"
    ),
    # 交互式工具无需配置硬超时，等待用户确认输入
    timeout=None,
    enabled_checker=None,
)(_raw_ask_clarification)

tavily_tool: ManagedTool = registry.register(
    category=ToolCategory.SEARCH,
    risk_level=RiskLevel.READ_ONLY,
    tags=["search", "web"],
    description="使用 Tavily 搜索引擎在互联网上实时搜索最新信息、新闻、网页资料等。",
    timeout=TAVILY_TIMEOUT_SECONDS + 5.0,  # 给网络层留出外层防御余量
    enabled_checker=lambda: bool(settings.tavily_api_key),
)(_raw_tavily_search)

#: **全量**工具列表（含当前配置下不可用的工具）。保留本列表供静态全量探针使用。
ALL_TOOLS: list[ManagedTool] = registry.all_tools()


def build_tools() -> list[ManagedTool]:
    """按当前配置装配要挂给 Agent 的 PydanticAI 工具列表。

    - `ask_user_clarification` 始终可用：它是交互协议的一部分（服务端会把它翻译成
      `clarification` 事件），不依赖任何外部服务；
    - `tavily_search` 只在配置了 `TAVILY_API_KEY` 时才挂：没有 key 时这个工具只会
      给模型返回「未配置」错误，白占一轮工具调用的 token，还会让模型以为自己能联网。

    每次调用都重新判断配置，因此调用方必须把结果纳入自己的缓存键——见
    `app/services/agent_factory.py` 的缓存键。
    """
    return registry.build_tools()


# 保持底层函数原样导出，确保老代码与直接测试函数调用的兼容性
ask_user_clarification = _raw_ask_clarification
tavily_search = _raw_tavily_search

__all__ = [
    "ALL_TOOLS",
    "ManagedTool",
    "RiskLevel",
    "TOOL_FAILURE_RESULT",
    "ToolCategory",
    "ToolErrorIsolation",
    "ToolRegistry",
    "ask_user_clarification",
    "build_tools",
    "clarification_tool",
    "registry",
    "tavily_search",
    "tavily_tool",
]
