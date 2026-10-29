"""运行时能力探针的响应契约。

能力不能由前端读环境变量或按默认配置猜：服务端装配工具时会按 `enabled_checker`
动态裁剪，唯一权威是 `app/tools/build_tools()` 的结果，所以这里返回装配结果的快照而非配置项列表。
"""

from pydantic import BaseModel, Field


class McpServerStatus(BaseModel):
    """一个 MCP server 的运行时状态。"""

    name: str = Field(description="server 名（也是工具名前缀）")
    tools: list[str] = Field(default_factory=list, description="该 server 提供、且已装配的工具名（不含前缀）")


class CapabilitiesResponse(BaseModel):
    """当前进程实际具备的能力。"""

    web_search: bool = Field(description="联网搜索工具（tavily_search）是否已装配")
    sandbox: bool = Field(
        default=False,
        description=(
            "隔离执行环境（sandbox_exec / sandbox_write_file / sandbox_read_file / sandbox_reset）"
            "是否已装配。前端据此决定要不要提「让我帮你跑段代码/处理文件」这类起步建议——"
            "承诺一个装不出来的能力比不给建议更糟。"
        ),
    )
    tools: list[str] = Field(
        default_factory=list,
        description="本轮装配给 Agent 的工具名（按注册顺序）；供前端与排障共用",
    )
    mcp_servers: list[McpServerStatus] = Field(
        default_factory=list,
        description="已配置且连通的 MCP server 及其工具；连不上的不在这个列表里",
    )
    mcp_error: str | None = Field(
        default=None,
        description=(
            "MCP 配置了但没能连上的原因；全部连通或未配置时为 null。"
            "单独给出这个字段是为了让「配了但不通」可被界面解释——"
            "只让工具从列表里消失，用户会以为配置从来没生效。"
        ),
    )
