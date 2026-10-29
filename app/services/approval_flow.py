"""审批（HITL）的状态桥：待审批请求 → 前端事件，客户端决定 → 续跑结果。

服务端无状态且 SSE 单向，审批只能分两段：本轮把拦下的调用交给客户端保存，续跑时按历史里的
悬空调用逐条取决定（缺失即拒绝）。工具说明与风险等级只向 `ToolCatalog` 要，本层不认识注册表与 MCP。
"""

import logging
from typing import Any

from pydantic_ai import (
    DeferredToolRequests,
    DeferredToolRequestsEvent,
    DeferredToolResults,
    ToolApproved,
    ToolDenied,
)
from pydantic_ai.messages import ModelMessage

from app.runtime.approval import DENIED_RESULT, all_deferred_ids
from app.runtime.catalog import ToolCatalog, get_catalog
from app.schemas.events import ApprovalRequiredEvent, ApprovalToolCall
from app.services.event_translation import _serialize_tool_args

logger = logging.getLogger(__name__)


def _remember_deferred(event: Any, sink: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN401
    """记住流里出现的待审批请求，作为终态取不到时的兜底。

    权威来源是 `run_result.output`（见 `_pending_from_result`），它也覆盖 capability 没解决任何调用的情况。
    """
    if isinstance(event, DeferredToolRequestsEvent):
        sink["requests"] = event.requests
    return sink


def _pending_from_result(
    run_result: Any,  # noqa: ANN401
    deferred: dict[str, Any],
) -> DeferredToolRequests | None:
    """从本轮终态取出待审批的调用；没有则返回 `None`。

    优先 `run_result.output`：它是 pydantic-ai 声明的本轮输出，`DeferredToolRequestsEvent` 只是流上的通知。
    """
    output = getattr(run_result, "output", None)
    if isinstance(output, DeferredToolRequests):
        return output
    requests = deferred.get("requests")
    return requests if isinstance(requests, DeferredToolRequests) else None


def build_approval_event(
    requests: DeferredToolRequests,
    *,
    catalog: ToolCatalog | None = None,
) -> ApprovalRequiredEvent:
    """把待审批请求翻译成前端事件，补上工具说明与风险等级。

    参数常常看不出后果（`{"id": 42}` 删的是什么？），所以这两个字段必需；查不到就留空，
    但不因此拒掉整轮（注册表可能在两轮之间被改配置）。`catalog` 缺省取进程级实现，参数注入优先。
    """
    source = catalog if catalog is not None else get_catalog()
    calls: list[ApprovalToolCall] = []
    for part in requests.approvals:
        tool_call_id = getattr(part, "tool_call_id", "") or ""
        tool_name = getattr(part, "tool_name", "") or ""
        description, risk_level = _describe_tool(tool_name, source)
        calls.append(
            ApprovalToolCall(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                args=_serialize_tool_args(getattr(part, "args", None)),
                description=description,
                risk_level=risk_level,
            )
        )
    return ApprovalRequiredEvent(tool_calls=calls)


def _describe_tool(tool_name: str, catalog: ToolCatalog) -> tuple[str, str]:
    """查工具的说明与风险等级；查不到返回空串（前端按「无说明」渲染）。

    风险等级在事件里是字符串，转成 `RiskLevel.value` 就只在这里做一次。
    """
    description, risk_level = catalog.describe(tool_name)
    return (description or "", risk_level.value if risk_level is not None else "")


def build_resume_results_for(
    approvals: dict[str, bool],
    history: list[ModelMessage],
) -> DeferredToolResults | None:
    """把客户端的审批决定变成 pydantic-ai 的续跑结果；不是续跑时返回 `None`。

    `DeferredToolResults` 必须覆盖每个待审批调用，缺失的按拒绝处理：没回应等于拒绝，否则关页面就是批准删除。
    拒绝也要显式给 `ToolDenied`：给了结果模型才会改走别的路，悬空的调用会让它再发一次。
    """
    pending_ids = all_deferred_ids(history)
    if not pending_ids:
        # 客户端带了 approvals 但没有对应调用（调用已有结果），按普通请求处理，别让多出的决定报错。
        logger.warning("请求带了审批决定，但历史里没有对应的待审批调用，按普通请求处理")
        return None

    results = DeferredToolResults()
    for call_id in pending_ids:
        approved = approvals.get(call_id, False)
        results.approvals[call_id] = ToolApproved() if approved else ToolDenied(DENIED_RESULT)
        if not approved:
            logger.info("用户拒绝执行工具调用 (tool_call_id=%s)", call_id)
    return results
