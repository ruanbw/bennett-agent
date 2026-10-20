"""运行时能力探针的响应契约。

前端空对话页的起步建议只应承诺**真正可用**的能力。此前建议是写死的四条，其中一条
要联网搜索；没配 Tavily 时点开只会得到一句「未配置」——示例被点开却答不了，比没有
示例更伤（见 `frontend/README.md`）。

能力不能由前端读环境变量或按默认配置猜：服务端装配工具时会按 `enabled_checker`
动态裁剪，唯一权威是 `app/tools/build_tools()` 的结果。因此这里返回的是**装配结果
的快照**，不是配置项列表。
"""

from pydantic import BaseModel, Field


class CapabilitiesResponse(BaseModel):
    """当前进程实际具备的能力。"""

    web_search: bool = Field(description="联网搜索工具（tavily_search）是否已装配")
    tools: list[str] = Field(
        default_factory=list,
        description="本轮装配给 Agent 的工具名（按注册顺序）；供前端与排障共用",
    )
