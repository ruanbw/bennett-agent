"""工具执行闸门（HITL 审批）：风险等级与用户预授权共同决定一次调用要不要问。

两道判据，任一不满足就拦：

1. **风险等级**：`SAFE_WRITE` 与 `DANGEROUS` 默认需要批准（分界线是不可撤销）；
2. **权限**：工具声明的 `permissions` 必须全部在用户当前授权集合里。用户在输入框选择
   「执行命令」后，`sandbox_exec` 就不再逐条询问；没选的权限仍然会拦下并弹出审批框。

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

from app.runtime.permissions import (
    DEFAULT_PERMISSIONS,
    AgentRunDeps,
    missing_permissions,
    permissions_from_metadata,
)
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
    "用户拒绝执行这次工具调用（或没有授予它需要的权限），它没有被执行。"
    "不要重试同一个调用，也不要声称已经完成；请说明你没有执行该操作，并询问用户希望怎么做。"
)


def requires_approval(
    risk_level: RiskLevel | str | None,
    *,
    required_permissions: Sequence[str] = (),
    granted_permissions: Sequence[str] | frozenset[str] | None = None,
) -> bool:
    """这次调用是否需要用户批准；**未知即拦**（含风险等级缺失）。

    判定顺序：

    1. 工具声明的权限没有全部授予 → 拦。这一条对只读工具同样生效：用户取消「联网访问」
       后，搜索工具也应该先问一次，而不是因为风险等级是只读就绕过用户的明确选择。
    2. 风险等级缺失或未知 → 拦。`None` 与未知字符串走同一条分支：metadata 在装配链上
       任何一处丢失（新的 toolset 包装层、pydantic-ai 升级后 `ToolDefinition.replace()`
       丢字段、MCP 分支不再补注解）都会走到这里；默认放行会让 HITL 静默失效。
    3. `SAFE_WRITE` / `DANGEROUS`：如果工具声明了权限且已全部授予，视为用户已预授权，
       放行；没有声明权限（无法判断属于哪类能力）则仍拦。
    4. 其余等级（`READ_ONLY` / `INTERACTIVE`）放行。
    """
    granted = DEFAULT_PERMISSIONS if granted_permissions is None else frozenset(granted_permissions)
    missing = missing_permissions(required_permissions, granted)
    if missing:
        logger.info("工具所需权限未授予，需要用户批准 (missing=%s)", missing)
        return True

    if risk_level is None:
        # 缺失不是「只读」：调用方拿不到分级时唯一安全的假设是「它可能需要批准」。
        logger.warning("工具缺少风险等级，按需要审批处理")
        return True
    if isinstance(risk_level, str):
        try:
            risk_level = RiskLevel(risk_level)
        except ValueError:
            # 未知等级按「需要审批」处理：新等级默认从严，漏审比多问一次代价大得多。
            logger.warning("未知的工具风险等级 %r，按需要审批处理", risk_level)
            return True
    if risk_level in APPROVAL_REQUIRED_LEVELS:
        # 已声明权限且全部授予 → 用户预授权覆盖了这次写入；没有权限声明则保持逐次询问。
        return not required_permissions
    return False


def _tool_risk_level(tool_def: ToolDefinition) -> RiskLevel | str | None:
    metadata = tool_def.metadata or {}
    value = metadata.get(RISK_METADATA_KEY)
    return value if isinstance(value, (RiskLevel, str)) else None


def _granted_permissions(ctx: RunContext[Any]) -> frozenset[str]:
    """从 run 依赖里取授权集合；没有 deps（测试、老调用点）时退回默认授权。"""
    deps = getattr(ctx, "deps", None)
    if isinstance(deps, AgentRunDeps):
        return deps.permissions
    raw = getattr(deps, "permissions", None)
    if isinstance(raw, (set, frozenset, list, tuple)):
        return frozenset(str(value) for value in raw)
    return DEFAULT_PERMISSIONS


class ApprovalGate(AbstractCapability[AgentRunDeps]):
    """在工具执行前按「风险等级 + 用户预授权」拦下需要批准的调用。

    用 `before_tool_execute` 而非 `Tool(requires_approval=True)`：工具动态装配，MCP 的风险等级
    运行时才知道。`ctx.tool_call_approved` 用 `is not True` 判断——`None`（无审批记录）也必须拦。
    风险等级缺失同样拦（见 `requires_approval`）：这是「未知即拦」在闸门里的落点。
    """

    async def before_tool_execute(
        self,
        ctx: RunContext[AgentRunDeps],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: Any,  # noqa: ANN401
    ) -> Any:  # noqa: ANN401
        if ctx.tool_call_approved is True:
            # 已经批准：放行。不再看风险等级——否则「批准了仍被拦」会让续跑死循环。
            return args

        risk = _tool_risk_level(tool_def)
        required = permissions_from_metadata(tool_def.metadata)
        granted = _granted_permissions(ctx)
        if not requires_approval(
            risk,
            required_permissions=required,
            granted_permissions=granted,
        ):
            logger.debug(
                "工具已获预授权或无需审批，直接执行 (tool=%s, required=%s, granted=%s)",
                call.tool_name,
                required,
                granted,
            )
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
