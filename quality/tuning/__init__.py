"""调优闭环：失败聚簇 → 补丁提议 → 重跑验证 → Pareto 择优。

对外只暴露四样东西：`cluster_failures`（共因识别）、`Proposer` 及其两个实现、
`optimize`（闭环入口）。内部实现（diff 工具、阈值常量）留在各自模块里，
避免调用方绕过闭环直接拼补丁。
"""

from __future__ import annotations

from quality.tuning.diagnose import assertion_name, cluster_failures
from quality.tuning.loop import BASE_CANDIDATE_ID, OBJECTIVE, RUNS_ROOT, optimize
from quality.tuning.propose import (
    HeuristicProposer,
    LLMProposer,
    Proposer,
    apply_unified_diff,
    make_unified_diff,
)

__all__ = [
    "BASE_CANDIDATE_ID",
    "OBJECTIVE",
    "RUNS_ROOT",
    "HeuristicProposer",
    "LLMProposer",
    "Proposer",
    "apply_unified_diff",
    "assertion_name",
    "cluster_failures",
    "make_unified_diff",
    "optimize",
]
