"""工具风险等级的执行闸门（HITL 审批）。

## 为什么要有这一层

`app/tools/registry.py` 里的 `RiskLevel` 长期只是一份**元数据**：全仓没有任何代码
读它做拦截，`build_tools()` 只看 `is_enabled`。这意味着任何标了 `DANGEROUS` 的工具
都是照样直接执行的——标签给了「这里有防护」的错觉，实际没有。这个模块把标签变成
真的闸门：`SAFE_WRITE` 与 `DANGEROUS` 在执行前必须拿到用户批准。

## 为什么不在这里阻塞等待用户

本项目是**无状态服务端**：历史由客户端每轮全量回放，服务端不保存会话。SSE 是单向
的，没有能让服务端「暂停等一个回答再继续」的通道；而在进程内挂起一个待审批的协程
在多 worker 部署下直接失效（下一次请求落在别的进程上）。

所以采用**分段续跑**：

1. 第一次请求：工具执行前抛 `ApprovalRequired`，pydantic-ai 把它变成
   `DeferredToolRequests`（本轮输出），**工具一次都没执行**。服务端下发一条
   `approval_required` 事件，本轮正常以 `done` 收尾。
2. 用户点了批准/拒绝 → 客户端发起新请求，带上**同一个** `tool_call_id` 与决定。
3. 服务端用这段历史重建消息（见 `chat_service.convert_chat_history` 的
   `resuming` 分支），把决定交给 `deferred_tool_results`，模型继续跑。

关键性质：**审批决定不靠服务端记住任何东西**。决定绑定在客户端回放的历史与它带来
的 `tool_call_id` 上，这也正是「客户端可以伪造历史」这个老问题的延续——见下面的
「伪造」一节。

## 伪造：客户端能给自己批准吗

能，但代价与收益都不对等，且我们做了能做的检查：

- `tool_call_id` 必须能在**本次请求**的历史里找到一条对应的工具调用，否则整条忽略
  （不让客户端凭空批准一个不存在的调用）；
- 批准只对**那一次调用**生效。客户端伪造历史时它已经能直接改对话内容了——审批防的
  不是「恶意客户端」，而是「模型自作主张执行破坏性操作」。这个威胁模型必须写在
  这里，否则后来者会误以为这层是安全边界。
- 真正的安全边界是工具自己的**输入校验**（例如删除接口必须带 `(id, user_id)` 双
  条件），不是审批。审批的作用是让人有机会看见并就具体参数做出决定。

## 哪些工具需要审批

由 `RiskLevel` 决定，见 `requires_approval()`。`READ_ONLY` 与 `INTERACTIVE` 不需要：
前者没有副作用，后者本身就是向用户提问（再审批一次是让用户确认「要不要提问」，
荒谬且会打断澄清流程）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ApprovalRequired
from pydantic_ai.messages import ModelMessage, ToolCallPart
from pydantic_ai.tools import ToolDefinition

from app.tools.registry import RiskLevel

logger = logging.getLogger(__name__)

#: 需要用户批准才能执行的风险等级。
#:
#: `SAFE_WRITE` 也算：它「低风险」但仍是**写入**，而写入是不可撤销的（本项目的删除
#: 是硬删除，没有回收站）。把「不可撤销」作为审批的分界线，比按「危险程度」划更清楚
#: ——可以随时问用户「这条记录要不要删」，但删完以后没法再问。
APPROVAL_REQUIRED_LEVELS: frozenset[RiskLevel] = frozenset({RiskLevel.SAFE_WRITE, RiskLevel.DANGEROUS})

#: 工具元数据里存风险等级的键（`ManagedTool` 构造时写入，见 `app/tools/registry.py`）。
RISK_METADATA_KEY = "risk_level"

#: 审批被拒绝时回灌给模型的文案。
#:
#: 必须明确「是用户拒绝的，不是工具坏了」：否则模型会把拒绝理解成一次故障，转而
#: 重试同一个调用（再弹一次审批框），或者编一个「已完成」的回答。
DENIED_RESULT = (
    "用户拒绝执行这次工具调用，它没有被执行。不要重试同一个调用，也不要声称已经完成；"
    "请说明你没有执行该操作，并询问用户希望怎么做。"
)


def requires_approval(risk_level: RiskLevel | str | None) -> bool:
    """这个风险等级是否需要用户批准。

    接受 `str`：MCP 工具的风险等级来自配置（字符串），本地工具来自枚举。
    """
    if risk_level is None:
        return False
    if isinstance(risk_level, str):
        try:
            risk_level = RiskLevel(risk_level)
        except ValueError:
            # 未知等级按「需要审批」处理：新等级默认从严，漏审比多问一次代价大得多。
            logger.warning("未知的工具风险等级 %r，按需要审批处理", risk_level)
            return True
    return risk_level in APPROVAL_REQUIRED_LEVELS


def _tool_risk_level(tool_def: ToolDefinition) -> RiskLevel | str | None:
    metadata = tool_def.metadata or {}
    value = metadata.get(RISK_METADATA_KEY)
    return value if isinstance(value, (RiskLevel, str)) else None


class ApprovalGate(AbstractCapability[None]):
    """在工具执行前按风险等级拦下需要批准的调用。

    用 `before_tool_execute` 而不是 `requires_approval=True`：后者是 `Tool` 的构造
    参数，而本项目的工具是**动态装配**的（`build_tools()` 每次按配置重新判断），且
    MCP 工具的风险等级在运行时才知道。挂在 capability 上只需一处判断，且能在拦截时
    读到完整的 `tool_def.metadata`。

    `ctx.tool_call_approved` 是 pydantic-ai 给本次调用的批准状态：续跑时它来自
    `DeferredToolResults`，第一次调用时为 `None`。只在「未明确批准」时拦截——写成
    `is not True` 而不是 `is False`，因为 `None`（没有审批记录）也必须拦。
    """

    async def before_tool_execute(
        self,
        ctx: RunContext[None],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: Any,  # noqa: ANN401
    ) -> Any:  # noqa: ANN401
        if ctx.tool_call_approved is True:
            # 已经批准：放行。不再看风险等级——否则「批准了仍被拦」会让续跑死循环。
            return args

        risk = _tool_risk_level(tool_def)
        if not requires_approval(risk):
            return args

        logger.info(
            "工具需要用户批准，本轮暂缓执行 (tool=%s, tool_call_id=%s, risk=%s)",
            call.tool_name,
            call.tool_call_id,
            risk,
        )
        raise ApprovalRequired()


# ---------------------------------------------------------------------
# 续跑：算出「历史里悬空的调用」
# ---------------------------------------------------------------------
def all_deferred_ids(history: Sequence[ModelMessage]) -> set[str]:
    """历史里所有「有调用、但没有对应结果」的工具调用 id。

    这是「哪些调用还等着决定」的**唯一权威判据**：不看客户端声明了什么，只看历史
    里确实有一个没有结果的调用。pydantic-ai 要求 `DeferredToolResults` 覆盖每一个
    这样的调用，缺失的直接抛 `ValueError`。

    拒绝也算「有结果」（`ToolDenied`），所以续跑一次之后它们就不再出现。
    """
    called: set[str] = set()
    returned: set[str] = set()
    for message in history:
        for part in getattr(message, "parts", ()):
            kind = getattr(part, "part_kind", "")
            call_id = getattr(part, "tool_call_id", None)
            if not isinstance(call_id, str) or not call_id:
                continue
            if kind == "tool-call":
                called.add(call_id)
            elif kind in {"tool-return", "retry-prompt"}:
                returned.add(call_id)
    return called - returned


__all__ = [
    "APPROVAL_REQUIRED_LEVELS",
    "DENIED_RESULT",
    "RISK_METADATA_KEY",
    "ApprovalGate",
    "all_deferred_ids",
    "requires_approval",
]
