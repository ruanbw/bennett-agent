"""旧路径垫片：实现已搬到 `app/runtime/guard.py`，新代码用 `app.runtime`。"""

from app.runtime.guard import (
    ESCALATE_AFTER_FAILURES,
    TOOL_FAILURE_RESULT,
    TRANSIENT_FAILURE_RESULT,
    ToolErrorIsolation,
    collect_tool_failures,
    failure_result_message,
    is_transient_tool_error,
    record_tool_failure,
)

__all__ = [
    "ESCALATE_AFTER_FAILURES",
    "TOOL_FAILURE_RESULT",
    "TRANSIENT_FAILURE_RESULT",
    "ToolErrorIsolation",
    "collect_tool_failures",
    "failure_result_message",
    "is_transient_tool_error",
    "record_tool_failure",
]
