"""运行时能力探针。

为什么需要它：空对话页的起步建议必须跟**当前装配结果**一致。前端拿不到
`build_tools()`，也不该按 `.env` 猜——占位 Key 与真正可用的 Key 在配置层面都是
「非空」，只靠前端判断会把不可用的能力当成可用。这里把唯一权威（工具注册中心的
实际装配结果）原样报给前端。

路由本身要求登录：能力清单会暴露服务端装了哪些工具，属于部署信息，不对外公开。
"""

from fastapi import APIRouter, Depends

from app.schemas.capabilities import CapabilitiesResponse
from app.services.auth import current_active_user
from app.tools import build_tools

router = APIRouter(prefix="/api", tags=["capabilities"])


def collect_capabilities() -> CapabilitiesResponse:
    """按实际装配结果生成能力清单（纯函数，便于单测）。"""
    tools = [tool.name for tool in build_tools()]
    return CapabilitiesResponse(web_search="tavily_search" in tools, tools=tools)


@router.get(
    "/capabilities",
    response_model=CapabilitiesResponse,
    summary="获取当前运行时能力",
    dependencies=[Depends(current_active_user)],
)
async def get_capabilities() -> CapabilitiesResponse:
    return collect_capabilities()
