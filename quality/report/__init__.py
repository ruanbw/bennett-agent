"""报告渲染层。

只暴露一个函数就够：报告是产物，不是可配置组件。调用方（`runner/report.py`、`cli.py`）
要的永远是「把这次运行变成一页能发给人的 HTML」，多一层 builder 只会多一套需要同步的参数。
"""

from __future__ import annotations

from quality.report.html import render

__all__ = ["render"]
