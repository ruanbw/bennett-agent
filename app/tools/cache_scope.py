"""旧路径垫片：实现已搬到 `app/runtime/scopes.py`，新代码用 `app.runtime`。"""

from app.runtime.scopes import cache_scope, current_cache_scope

__all__ = ["cache_scope", "current_cache_scope"]
