"""旧路径垫片：实现已搬到 `app/runtime/catalog.py`，新代码用 `app.runtime`。"""

from app.runtime.catalog import (
    DefaultToolCatalog,
    ToolCatalog,
    get_catalog,
    set_catalog_for_tests,
)

__all__ = [
    "DefaultToolCatalog",
    "ToolCatalog",
    "get_catalog",
    "set_catalog_for_tests",
]
