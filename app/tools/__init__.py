"""统一工具注册中心与导出列表：集中编目（分类、风险等级、标签）、超时与按环境裁剪。

装配层，import 期不注册任何工具：`get_registry()` 首次调用才把实现拉起来——否则服务层与测试
只要碰到 `app.tools` 的任一子模块，就顺带把沙箱与 Tavily 的实现栈装进内存。
工具级超时是外层防线，必须大于工具内部最坏耗时：pydantic-ai 的 `anyio.fail_after` 先触发时
抛 `ModelRetry`，工具里写的错误文案全部不可达。数字只在 `app/tools/tavily_search.py` 派生。
"""

import importlib
from typing import Any

from app.runtime.guard import (
    ESCALATE_AFTER_FAILURES,
    TOOL_FAILURE_RESULT,
    ToolErrorIsolation,
    collect_tool_failures,
    failure_result_message,
)
from app.runtime.tool_registry import (
    ManagedTool,
    RiskLevel,
    ToolCategory,
    ToolRegistry,
)
from app.services.sandbox.base import MAX_EXEC_TIMEOUT_S

# `app.tools.registry` 既是垫片模块、又是包级单例的旧导出名：导入子模块会把同名包属性换成模块
# （原实现靠「先 import 子模块、再用模块级赋值覆盖」避开），这里复现同样的覆盖顺序。
importlib.import_module("app.tools.registry")
globals().pop("registry", None)

#: 沙箱工具的外层防线超时，必须严格大于工具内部能等的最长时间（`MAX_EXEC_TIMEOUT_S` 是
#: 模型能调的 `timeout_s` 上限，另加建机与传输余量）：pydantic-ai 的超时抛成 `ModelRetry`，
#: 先于内部收尾触发时工具里的中文建议就全不可达。
SANDBOX_TOOL_TIMEOUT_SECONDS = MAX_EXEC_TIMEOUT_S + 60.0

#: 旧导出名 → 注册表里的工具名。工具对象随注册表惰性产生，只能经 `__getattr__` 取。
_TOOL_EXPORTS: dict[str, str] = {
    "clarification_tool": "ask_user_clarification",
    "current_time_tool": "get_current_time",
    "tavily_tool": "tavily_search",
    "sandbox_exec_tool": "sandbox_exec",
    "sandbox_write_file_tool": "sandbox_write_file",
    "sandbox_read_file_tool": "sandbox_read_file",
    "sandbox_reset_tool": "sandbox_reset",
}

#: 原样导出的底层实现（测试与老代码直接调本体，不走工具包装）：导出名 → 所在模块。
_RAW_FUNCTION_MODULES: dict[str, str] = {
    "ask_user_clarification": "app.tools.ask_user_clarification",
    "get_current_time": "app.tools.current_time",
    "tavily_search": "app.tools.tavily_search",
    "sandbox_exec": "app.tools.sandbox",
    "sandbox_write_file": "app.tools.sandbox",
    "sandbox_read_file": "app.tools.sandbox",
    "sandbox_reset": "app.tools.sandbox",
}

#: 全局工具注册中心单例，`get_registry()` 首次调用时装配
_REGISTRY: ToolRegistry | None = None


def _register_all(registry: ToolRegistry) -> None:
    """把全部工具注册进 `registry`。元数据只在这一次编目，装配与能力探针都读它。"""
    from app.config import is_usable_api_key, settings
    from app.services.sandbox import is_sandbox_enabled
    from app.tools.ask_user_clarification import ask_user_clarification as _raw_ask_clarification
    from app.tools.current_time import get_current_time as _raw_get_current_time
    from app.tools.sandbox import (
        sandbox_exec as _raw_sandbox_exec,
    )
    from app.tools.sandbox import (
        sandbox_read_file as _raw_sandbox_read_file,
    )
    from app.tools.sandbox import (
        sandbox_reset as _raw_sandbox_reset,
    )
    from app.tools.sandbox import (
        sandbox_write_file as _raw_sandbox_write_file,
    )
    from app.tools.tavily_search import (
        TAVILY_TOOL_TIMEOUT_SECONDS,
    )
    from app.tools.tavily_search import (
        tavily_search as _raw_tavily_search,
    )

    registry.register(
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

    registry.register(
        category=ToolCategory.SYSTEM,
        risk_level=RiskLevel.READ_ONLY,
        tags=["time", "clock"],
        description=(
            "获取当前的日期、时间与星期，并附带时区与 UTC 时间。"
            "当问题涉及「今天」「现在」「今年」「最近」「还有几天」等与当前时间相关的内容时调用。"
        ),
        # 纯本地计算，没有外部依赖，也不存在慢调用，因此不需要超时与启用开关。
        timeout=None,
        enabled_checker=None,
    )(_raw_get_current_time)

    registry.register(
        category=ToolCategory.SEARCH,
        risk_level=RiskLevel.READ_ONLY,
        tags=["search", "web"],
        description="使用 Tavily 搜索引擎在互联网上实时搜索最新信息、新闻、网页资料等。",
        timeout=TAVILY_TOOL_TIMEOUT_SECONDS,
        enabled_checker=lambda: is_usable_api_key(settings.tavily_api_key),
    )(_raw_tavily_search)

    # ---------------------------------------------------------------------
    # 沙箱：四个工具，签名里没有任何 id。
    # 分级按「能不能撤销」而非「有多危险」（见 `app/runtime/approval.py`）：命令全可撤销，故不逐条
    # 审批；身份由作用域在构造上保证（`app/runtime/scopes.py`），只有 `sandbox_reset` 需审批。
    # ---------------------------------------------------------------------

    registry.register(
        category=ToolCategory.DATA,
        risk_level=RiskLevel.READ_ONLY,
        tags=["sandbox", "code", "compute"],
        description=(
            "在用户自己的执行环境（同一用户跨会话共用一台机器）里跑一条 shell 命令，返回退出码与输出。"
            "适用于：跑 Python/脚本、"
            "处理数据与文件、安装依赖、运行测试、查看文件内容。"
            "环境是持久的（同一会话内文件和已装的包都在），但没有持久 shell 会话："
            "每次都是新进程，`cd` 与 `export` 不会留到下一次，请用绝对路径或 `命令 && 命令` 串起来。"
        ),
        timeout=SANDBOX_TOOL_TIMEOUT_SECONDS,
        enabled_checker=is_sandbox_enabled,
    )(_raw_sandbox_exec)

    registry.register(
        category=ToolCategory.DATA,
        risk_level=RiskLevel.READ_ONLY,
        tags=["sandbox", "files"],
        description=(
            "把文本内容写入用户执行环境里的文件（父目录会自动创建；同一用户的其它会话也能看到）。"
            "适合把脚本、配置或用户给的数据落成文件，再用 sandbox_exec 跑它。路径必须是以 "
            "/home/sandbox/workspace 开头的绝对路径。"
        ),
        timeout=60.0,
        enabled_checker=is_sandbox_enabled,
    )(_raw_sandbox_write_file)

    registry.register(
        category=ToolCategory.DATA,
        risk_level=RiskLevel.READ_ONLY,
        tags=["sandbox", "files"],
        description=(
            "读取用户执行环境里的文件内容（超过 max_bytes 会报错而不是截断——"
            "截断的二进制或 JSON 会让你在坏文件上继续推理）。大文件请用 sandbox_exec 配合 "
            "head/tail/grep 分段看。"
        ),
        timeout=60.0,
        enabled_checker=is_sandbox_enabled,
    )(_raw_sandbox_read_file)

    registry.register(
        category=ToolCategory.DATA,
        risk_level=RiskLevel.SAFE_WRITE,
        tags=["sandbox", "reset"],
        description=(
            "重置用户的执行环境：销毁现有机器，下次执行时会重新创建一个干净的。"
            "注意这台机器是同一用户跨会话共用的，重置会影响该用户的所有会话。"
            "只在环境已经被弄乱、或用户明确要求从头开始时使用——已安装的依赖与工作目录里的文件都会丢。"
        ),
        timeout=120.0,
        enabled_checker=is_sandbox_enabled,
    )(_raw_sandbox_reset)


def get_registry() -> ToolRegistry:
    """取全局注册表单例；首次调用才注册工具（import 期注册会把整条实现栈拉进每个服务）。"""
    global _REGISTRY
    if _REGISTRY is None:
        registry = ToolRegistry()
        _register_all(registry)
        _REGISTRY = registry
    return _REGISTRY


def set_registry_for_tests(registry: ToolRegistry | None) -> None:
    """替换/清空注册表单例。只给测试用（`None` 还原成「下次调用重新注册」）。"""
    global _REGISTRY
    _REGISTRY = registry


def reset_registry() -> None:
    """丢弃当前注册表，下次 `get_registry()` 重新编目（进程内改过配置之后用）。"""
    set_registry_for_tests(None)


def build_tools() -> list[ManagedTool]:
    """按当前配置装配要挂给 Agent 的 PydanticAI 工具列表。

    `tavily_search` 无 key 就不挂（否则只会白占一轮 token），其余始终可用：clarification 是交互协议
    的一部分，`get_current_time` 纯本地。每次调用都重新判断配置，调用方须把结果纳入缓存键。
    """
    return get_registry().build_tools()


def __getattr__(name: str) -> Any:
    """旧导出名一律懒加载：工具对象与底层实现都等到真正取值时才 import。"""
    if name == "registry":
        return get_registry()
    if name == "ALL_TOOLS":
        return get_registry().all_tools()
    if name in _TOOL_EXPORTS:
        return get_registry().get(_TOOL_EXPORTS[name])
    module_name = _RAW_FUNCTION_MODULES.get(name)
    if module_name is not None:
        return getattr(importlib.import_module(module_name), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ALL_TOOLS",
    "ManagedTool",
    "RiskLevel",
    "ESCALATE_AFTER_FAILURES",
    "TOOL_FAILURE_RESULT",
    "collect_tool_failures",
    "failure_result_message",
    "ToolCategory",
    "ToolErrorIsolation",
    "ToolRegistry",
    "ask_user_clarification",
    "build_tools",
    "clarification_tool",
    "current_time_tool",
    "get_current_time",
    "get_registry",
    "reset_registry",
    "sandbox_exec",
    "sandbox_exec_tool",
    "sandbox_read_file",
    "sandbox_read_file_tool",
    "sandbox_reset",
    "sandbox_reset_tool",
    "sandbox_write_file",
    "sandbox_write_file_tool",
    "registry",
    "set_registry_for_tests",
    "tavily_search",
    "tavily_tool",
]
