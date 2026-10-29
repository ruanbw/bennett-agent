"""工具风险等级的执行闸门（HITL 审批）：`SAFE_WRITE` 与 `DANGEROUS` 执行前必须拿到用户批准。

服务端无状态且 SSE 单向，所以不阻塞等待：首轮抛 `ApprovalRequired` 变成 `DeferredToolRequests`，
客户端带同一个 `tool_call_id` 续跑。审批防「模型自作主张」，真正的安全边界是工具自己的输入校验。
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

from app.runtime.tool_registry import RiskLevel

logger = logging.getLogger(__name__)

#: 需要用户批准的风险等级。`SAFE_WRITE` 也算：它低风险但仍是写入，而写入不可撤销
#: （本项目是硬删除，没有回收站）。分界线是「不可撤销」而非「危险程度」。
APPROVAL_REQUIRED_LEVELS: frozenset[RiskLevel] = frozenset({RiskLevel.SAFE_WRITE, RiskLevel.DANGEROUS})

#: 工具元数据里存风险等级的键（`ManagedTool` 构造时写入，见 `app/runtime/tool_registry.py`）。
RISK_METADATA_KEY = "risk_level"

#: 审批被拒时回灌给模型的文案。必须说明「是用户拒绝、不是工具坏了」，否则模型会把
#: 拒绝当成一次故障：重试同一个调用（再弹一次审批框），或者编一个「已完成」的回答。
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

    用 `before_tool_execute` 而非 `Tool(requires_approval=True)`：工具动态装配，MCP 的风险等级
    运行时才知道。`ctx.tool_call_approved` 用 `is not True` 判断——`None`（无审批记录）也必须拦。
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

    唯一权威判据：不看客户端声明了什么，只看历史。pydantic-ai 要求 `DeferredToolResults`
    覆盖每一个这样的调用，缺一个直接 `ValueError`；拒绝也算有结果，所以续跑一次后不再出现。
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
