"""统一工具注册中心与导出列表。

基于 PydanticAI 原生 Tool 体系进行扩展与治理，提供集中编目、元数据管理
（分类、风险等级、标签）、超时配置以及按环境动态裁剪装配能力。

## 工具级超时是**外层防线**，必须大于工具内部自己的最坏耗时

pydantic-ai 的 `FunctionToolsetTool.call_tool` 用 `anyio.fail_after(timeout)` 包裹工具，
超时时抛 `ModelRetry`（不是普通异常，也不是工具能捕获的错误结果）。所以它一旦先于
工具内部收尾触发，工具里精心写的错误文案全部不可达，还会额外消耗一次模型重试预算。
具体数字由 `app/tools/tavily_search.py` 的 `TAVILY_TOOL_TIMEOUT_SECONDS` 单点派生——
不要把常数直接写在这里，那正是之前「外层 25s < 内层最坏 40.5s」的成因。
"""

from app.config import is_usable_api_key, settings
from app.services.rag.embeddings import EMBEDDING_TIMEOUT_SECONDS
from app.services.sandbox import is_sandbox_enabled
from app.services.sandbox.base import MAX_EXEC_TIMEOUT_S
from app.tools.ask_user_clarification import ask_user_clarification as _raw_ask_clarification
from app.tools.current_time import get_current_time as _raw_get_current_time
from app.tools.guard import (
    ESCALATE_AFTER_FAILURES,
    TOOL_FAILURE_RESULT,
    ToolErrorIsolation,
    collect_tool_failures,
    failure_result_message,
)
from app.tools.knowledge_search import (
    is_knowledge_base_enabled,
)
from app.tools.knowledge_search import (
    search_knowledge_base as _raw_search_knowledge_base,
)
from app.tools.registry import (
    ManagedTool,
    RiskLevel,
    ToolCategory,
    ToolRegistry,
)
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

#: 全局工具注册中心单例
registry = ToolRegistry()

#: 沙箱工具的**外层防线**超时。必须严格大于工具内部自己能等的最长时间：
#: 模型可以把 `timeout_s` 调到 `MAX_EXEC_TIMEOUT_S`（900s），再加建机与文件传输的余量。
#: pydantic-ai 的超时是包在工具外层、抛成 `ModelRetry` 的，一旦先于内部收尾触发，
#: 工具里写好的中文建议就全部不可达（与 tavily 那段注释同一个坑）。
SANDBOX_TOOL_TIMEOUT_SECONDS = MAX_EXEC_TIMEOUT_S + 60.0

#: 知识库检索的工具级超时（外层防线）。必须**严格大于** embedding 自己的
#: `EMBEDDING_TIMEOUT_SECONDS`：pydantic-ai 的超时会抛成 `ModelRetry`，先于工具内部
#: 收尾触发时，工具里写的中文建议全部不可达（与 tavily 那段注释同一个坑）。
KNOWLEDGE_TOOL_TIMEOUT_SECONDS = EMBEDDING_TIMEOUT_SECONDS + 5.0

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

current_time_tool: ManagedTool = registry.register(
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

tavily_tool: ManagedTool = registry.register(
    category=ToolCategory.SEARCH,
    risk_level=RiskLevel.READ_ONLY,
    tags=["search", "web"],
    description="使用 Tavily 搜索引擎在互联网上实时搜索最新信息、新闻、网页资料等。",
    timeout=TAVILY_TOOL_TIMEOUT_SECONDS,
    enabled_checker=lambda: is_usable_api_key(settings.tavily_api_key),
)(_raw_tavily_search)

knowledge_tool: ManagedTool = registry.register(
    category=ToolCategory.DATA,
    risk_level=RiskLevel.READ_ONLY,
    tags=["knowledge", "rag", "documents"],
    description=(
        "在用户自己上传的知识库文档中检索相关内容。当问题涉及用户的文档、手册、规章制度、产品资料等内容时调用。"
    ),
    # 检索 = 一次 embedding 调用 + 一次全库扫描；给一个上限，避免 embedding 服务卡住时
    # 整轮 SSE 被拖到总预算耗尽。这个值要大于 embedding 自己的超时（15s），否则模型
    # 看到的是 pydantic-ai 的英文超时文案而不是本工具的可执行建议。
    timeout=KNOWLEDGE_TOOL_TIMEOUT_SECONDS,
    enabled_checker=is_knowledge_base_enabled,
)(_raw_search_knowledge_base)

# ---------------------------------------------------------------------
# 沙箱：四个工具，**签名里没有任何 id**。
#
# 为什么不是 DANGEROUS：审批闸门的分界线是「能不能撤销」而不是「有多危险」
# （见 `app/tools/approval.py`）。沙箱内的命令全部可撤销——机器可弃、产物在导出后
# 才销毁——所以逐条命令审批只会让沙箱变成不可用的点选框。真正的风险落在入口：
# 「机器属于谁」由作用域在构造上保证（`app/tools/sandbox_scope.py`），而不是靠每条命令审批。
#
# 唯一需要审批的是 `sandbox_reset`（SAFE_WRITE）：它会销毁当前会话的机器。
# 产物会先导出，但这个动作不可逆，值得打断用户一次。
# ---------------------------------------------------------------------

sandbox_exec_tool: ManagedTool = registry.register(
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

sandbox_write_file_tool: ManagedTool = registry.register(
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

sandbox_read_file_tool: ManagedTool = registry.register(
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

sandbox_reset_tool: ManagedTool = registry.register(
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

#: **全量**工具列表（含当前配置下不可用的工具）。保留本列表供静态全量探针使用。
ALL_TOOLS: list[ManagedTool] = registry.all_tools()


def build_tools() -> list[ManagedTool]:
    """按当前配置装配要挂给 Agent 的 PydanticAI 工具列表。

    - `ask_user_clarification` 始终可用：它是交互协议的一部分（服务端会把它翻译成
      `clarification` 事件），不依赖任何外部服务；
    - `get_current_time` 始终可用：纯本地计算，没有可失败的分支；模型自己的时间感
      是过期的，缺了它「今天几号」只能靠猜；
    - `tavily_search` 只在配置了 `TAVILY_API_KEY` 时才挂：没有 key 时这个工具只会
      给模型返回「未配置」错误，白占一轮工具调用的 token，还会让模型以为自己能联网；
    - `search_knowledge_base` 只在配置了 `EMBEDDING_MODEL` 时才挂，理由同上。
      「有没有上传文档」不参与判定：那是用户数据，不是部署能力，而且随时会变。

    每次调用都重新判断配置，因此调用方必须把结果纳入自己的缓存键——见
    `app/services/agent_factory.py` 的缓存键。
    """
    return registry.build_tools()


# 保持底层函数原样导出，确保老代码与直接测试函数调用的兼容性
ask_user_clarification = _raw_ask_clarification
get_current_time = _raw_get_current_time
search_knowledge_base = _raw_search_knowledge_base
tavily_search = _raw_tavily_search
sandbox_exec = _raw_sandbox_exec
sandbox_write_file = _raw_sandbox_write_file
sandbox_read_file = _raw_sandbox_read_file
sandbox_reset = _raw_sandbox_reset

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
    "is_knowledge_base_enabled",
    "knowledge_tool",
    "search_knowledge_base",
    "sandbox_exec",
    "sandbox_exec_tool",
    "sandbox_read_file",
    "sandbox_read_file_tool",
    "sandbox_reset",
    "sandbox_reset_tool",
    "sandbox_write_file",
    "sandbox_write_file_tool",
    "registry",
    "tavily_search",
    "tavily_tool",
]
