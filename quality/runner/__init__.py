"""执行层：跑套件、算指标、落盘、对基线做门禁。

四个模块的边界是按「谁读谁」划的：

- `executor` 只负责把样本跑完并交出 `(report, verdicts)`，不做任何判分口径的决定
  （`passed` 只由 `graders.base.judge` 写）；
- `metrics` 只读 `TaskSummary` / `SampleVerdict`，是纯函数，可以脱开执行器单测；
- `report` 只管序列化，不参与计算；
- `gate` 只读两份 `RunReport` 的指标与指纹，不认识 target 也不认识 suite 文件。

这样调优闭环（`tuning/loop.py`）能复用 `executor` + `metrics` 而不触发落盘与门禁，
报告渲染改版也不会碰到执行逻辑。
"""

from __future__ import annotations

from quality.runner.executor import run_suite
from quality.runner.gate import GateOutcome, evaluate_gate
from quality.runner.metrics import compute_metrics, summarise
from quality.runner.report import render_html, write_json

__all__ = [
    "GateOutcome",
    "compute_metrics",
    "evaluate_gate",
    "render_html",
    "run_suite",
    "summarise",
    "write_json",
]
