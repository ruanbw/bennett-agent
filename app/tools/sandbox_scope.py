"""旧路径垫片：实现已搬到 `app/runtime/scopes.py`，新代码用 `app.runtime`。"""

from app.runtime.scopes import SandboxScope, current_sandbox_scope, sandbox_scope

__all__ = ["SandboxScope", "current_sandbox_scope", "sandbox_scope"]
