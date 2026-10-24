"""运行时能力探针。

为什么需要它：空对话页的起步建议必须跟**当前装配结果**一致。前端拿不到
`build_tools()`，也不该按 `.env` 猜——占位 Key 与真正可用的 Key 在配置层面都是
「非空」，只靠前端判断会把不可用的能力当成可用。这里把唯一权威（工具注册中心的
实际装配结果）原样报给前端。

路由本身要求登录：能力清单会暴露服务端装了哪些工具，属于部署信息，不对外公开。
"""

from fastapi import APIRouter, Depends

from app.schemas.capabilities import CapabilitiesResponse, McpServerStatus
from app.services.auth import current_active_user
from app.tools import build_tools
from app.tools.mcp_client import get_mcp_registry

router = APIRouter(prefix="/api", tags=["capabilities"])


def collect_capabilities() -> CapabilitiesResponse:
    """按实际装配结果生成能力清单（纯函数，便于单测）。

    MCP 部分读的是**已缓存**的发现结果：端点本身是同步的，而发现要建连。
    启动时已经 await 过一次（`app/server.py:_discover_mcp_tools`），所以正常路径下
    这里拿到的是真实清单；还没发现过时列表为空——那比让端点去联网更可预期。
    """
    tools = [tool.name for tool in build_tools()]

    registry = get_mcp_registry()
    grouped: dict[str, list[str]] = {}
    for info in registry.cached_tools():
        grouped.setdefault(info.server, []).append(info.name)

    return CapabilitiesResponse(
        web_search="tavily_search" in tools,
        # 沙箱是「每会话一台机器」的能力，开关在 settings。关着的时候模型看不到这四个工具，
        # 前端也不应该提「跑代码」这类建议；开了但当前请求没带 session_id 时工具会自己拒绝，
        # 那是请求级问题，不该把部署能力报成不可用。
        sandbox=any(name.startswith("sandbox_") for name in tools),
        tools=tools,
        mcp_servers=[McpServerStatus(name=name, tools=names) for name, names in sorted(grouped.items())],
        mcp_error=registry.discovery_error,
    )


@router.get(
    "/capabilities",
    response_model=CapabilitiesResponse,
    summary="获取当前运行时能力",
    dependencies=[Depends(current_active_user)],
)
async def get_capabilities() -> CapabilitiesResponse:
    return collect_capabilities()
