"""旧路径垫片：实现已搬到 `app/runtime/approval.py`，新代码用 `app.runtime`。"""

from app.runtime.approval import (
    APPROVAL_REQUIRED_LEVELS,
    DENIED_RESULT,
    RISK_METADATA_KEY,
    ApprovalGate,
    ApprovalRequired,
    all_deferred_ids,
    requires_approval,
)

__all__ = [
    "APPROVAL_REQUIRED_LEVELS",
    "DENIED_RESULT",
    "RISK_METADATA_KEY",
    "ApprovalGate",
    "ApprovalRequired",
    "all_deferred_ids",
    "requires_approval",
]
