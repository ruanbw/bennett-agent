"""调优闭环：诊断 → 提议 → 重跑 → 按 Pareto 双目标择优。

为什么是 Pareto 而不是「分数最高者胜」：

- 分数最高常常来自「把提示词堆得更长更细」，代价是每轮 token 成本上涨。企业真正要的
  是「在成本不涨的前提下把分数抬起来」，所以目标一直是二维的：`weighted_score` 最大、
  `cost_usd` 最小；
- 但 Pareto 前沿本身不能当结论用：前沿上可能有分数更高、安全性更低的点（安全类用例
  被放松了措辞）。所以**入选还多一道硬门槛**：`safety` 与 `weighted_score` 都不得低于
  基线。前沿用于解释「为什么选了它」，门槛用于解释「为什么淘汰它」。

其它口径：

- 基线（`base_report`）本身作为候选 `base` 参与比较。这样「没有任何提案可入选」时
  结论是明确的「保持基线」，而不是 `selected=None` 让人以为调优失败；
- 每份补丁都相对于**父候选的提示词**独立 apply，一次重跑只验证一份补丁：
  组合补丁（A+B）再验证会把「谁带来的收益」混在一起，lineage 就说不清了；
- 预算耗尽即停，`stopped_reason` 记原因；正常跑完 iterations 时 `stopped_reason` 为空串。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from quality.audit import AuditLog
from quality.budget import Budget
from quality.graders.base import Grader
from quality.models import (
    Candidate,
    Patch,
    RunReport,
    SampleVerdict,
    TuningReport,
    fingerprint,
)
from quality.targets.base import Target
from quality.tuning.diagnose import cluster_failures
from quality.tuning.propose import Proposer, apply_unified_diff

if TYPE_CHECKING:  # pragma: no cover - 用例装载由并行任务实现，只在类型检查时导入
    from quality.suites.loader import Suite

#: 候选提示词落盘根目录。放在仓库里不是「构建产物」，而是人审证据：调优结论必须能复盘。
RUNS_ROOT = Path("quality/runs/tuning")

#: 基线的候选 id。固定名字让它在前端/报告里可读，也方便下游按 id 找父节点。
BASE_CANDIDATE_ID = "base"

#: 浮点比较的容差。分数是加权平均，0.1+0.2 这类误差不该被当成「退步」。
EPSILON = 1e-9

#: 目标函数的人话描述，写进 TuningReport 让人不用读代码就知道优化的是什么。
OBJECTIVE = "max weighted_score, min cost_usd；入选额外要求 safety 不降 且 weighted_score 不降"

Runner = Callable[..., Any]


def _resolve_run_suite() -> Runner:
    """延迟解析执行器。

    两个理由：执行器会把判分器与指标模块整条链拉进来，顶部 import 会让「只想读聚簇逻辑」
    的调用方也被迫装载它们；同时这是测试替换执行器的唯一缝隙（不必为测试加公开参数）。
    """
    from quality.runner.executor import run_suite

    return run_suite


def _new_run_id(prompt_text: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{fingerprint(prompt_text)[:8]}"


def _metrics_of(report: RunReport) -> dict[str, float]:
    """只留数值型指标：`Candidate.metrics` 是 `dict[str, float]`，混入字符串会让报告炸。"""
    return {key: float(value) for key, value in report.metrics.items() if isinstance(value, int | float)}


def _score_of(candidate: Candidate) -> float:
    return candidate.metrics.get("weighted_score", 0.0)


def _safety_of(candidate: Candidate) -> float:
    return candidate.metrics.get("safety", 0.0)


def _not_worse(metrics: dict[str, float], base_score: float, base_safety: float) -> bool:
    """入选的硬门槛：安全与总分都不降。先看安全——安全是「不可交易」的那一个。

    入参是 metrics 而不是 `Candidate`：重跑刚结束、还没造出候选时也要能判门槛。
    """
    return (
        metrics.get("safety", 0.0) >= base_safety - EPSILON
        and metrics.get("weighted_score", 0.0) >= base_score - EPSILON
    )


def _dominates(left: Candidate, right: Candidate) -> bool:
    """left 在双目标上支配 right：分数不低、成本不高，且至少一项严格更好。"""
    score_ok = _score_of(left) >= _score_of(right) - EPSILON
    cost_ok = left.cost_usd <= right.cost_usd + EPSILON
    strictly_better = _score_of(left) > _score_of(right) + EPSILON or left.cost_usd < right.cost_usd - EPSILON
    return score_ok and cost_ok and strictly_better


def _pareto_front(candidates: list[Candidate]) -> list[str]:
    """非支配集：没有任何候选在「分数更高 + 成本更低」上全面压过它。"""
    front = [
        item.id for item in candidates if not any(_dominates(other, item) for other in candidates if other is not item)
    ]
    return sorted(front)


def _select(candidates: list[Candidate], base_score: float, base_safety: float) -> Candidate:
    """在「不退化」的候选里挑分数最高的；同分选便宜的；再同分选 id 靠前的（确定性）。"""
    eligible = [item for item in candidates if _not_worse(item.metrics, base_score, base_safety)]
    # base 一定在 eligible 里，所以这里不会空。按 (-分数, 成本, id) 排序保证同一份输入
    # 永远选出同一个候选，调优结果才可复盘。
    return sorted(eligible, key=lambda item: (-_score_of(item), item.cost_usd, item.id))[0]


def _write_candidate(directory: Path, candidate_id: str, prompt_text: str) -> Path:
    path = directory / "candidates" / f"{candidate_id}.jinja"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(prompt_text, encoding="utf-8")
    return path


def _with_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") or not text else text + "\n"


async def optimize(
    target_factory: Callable[[str], Target],
    suite: Suite,
    base_report: RunReport,
    base_verdicts: list[SampleVerdict],
    *,
    proposer: Proposer,
    prompt_text: str,
    iterations: int = 2,
    samples: int = 1,
    graders: list[Grader] | None = None,
    budget: Budget | None = None,
    audit: AuditLog | None = None,
) -> TuningReport:
    """跑 ``iterations`` 轮「诊断 → 提议 → 重跑」，返回带 lineage 与 Pareto 前沿的结论。"""
    run_suite = _resolve_run_suite()
    run_id = _new_run_id(prompt_text)
    directory = RUNS_ROOT / run_id
    directory.mkdir(parents=True, exist_ok=True)

    base_metrics = _metrics_of(base_report)
    base_score = base_metrics.get("weighted_score", 0.0)
    base_safety = base_metrics.get("safety", 0.0)

    base_candidate = Candidate(
        id=BASE_CANDIDATE_ID,
        patch=None,
        metrics=base_metrics,
        cost_usd=base_report.cost_usd,
        passed=True,
        prompt_sha=fingerprint(prompt_text),
    )
    candidates: list[Candidate] = [base_candidate]
    # 候选提示词与它的运行结果都留在内存里：下一轮诊断要看「当前最好那个」的失败。
    prompts: dict[str, str] = {BASE_CANDIDATE_ID: _with_trailing_newline(prompt_text)}
    results: dict[str, tuple[RunReport, list[SampleVerdict]]] = {
        BASE_CANDIDATE_ID: (base_report, base_verdicts),
    }
    _write_candidate(directory, BASE_CANDIDATE_ID, prompts[BASE_CANDIDATE_ID])

    if audit is not None:
        audit.append(
            "tune.start",
            run_id,
            suite=suite.id,
            iterations=iterations,
            samples=samples,
            base_run=base_report.run_id,
        )

    current = base_candidate
    stopped_reason = ""
    counter = 0

    for step in range(iterations):
        reason = budget.check() if budget is not None else None
        if reason:
            stopped_reason = f"预算中止：{reason}"
            break

        current_report, current_verdicts = results[current.id]
        clusters = cluster_failures(current_report, current_verdicts)
        if not clusters:
            stopped_reason = f"第 {step + 1} 轮没有可聚簇的失败，无需继续调优"
            break

        patches: list[Patch] = await proposer.propose(clusters, prompts[current.id])
        if not patches:
            stopped_reason = f"第 {step + 1} 轮提议者没有给出可用补丁"
            break

        exhausted = False
        applied = 0
        for patch in patches:
            if budget is not None and budget.check():
                stopped_reason = f"预算中止：{budget.stopped_reason}"
                exhausted = True
                break
            counter += 1
            candidate_id = f"c{counter:02d}"
            try:
                candidate_prompt = apply_unified_diff(prompts[current.id], patch.diff)
            except ValueError as exc:
                # 补丁上下文错位就跳过：宁可少一个候选，也不要产出一个语义错位的提示词。
                if audit is not None:
                    audit.append("tune.patch_skipped", f"{run_id}/{patch.id}", outcome="error", reason=str(exc))
                continue

            text = _with_trailing_newline(candidate_prompt)
            applied += 1
            _write_candidate(directory, candidate_id, text)

            target = target_factory(text)
            report, verdicts = await run_suite(
                target,
                suite,
                samples=samples,
                # 候选必须用**与基线同一套判分器**重跑：少了 rubric judge，候选的
                # 缺项会被按 0 计，于是「提示词变好」看起来像「全盘崩了」。
                graders=graders,
                budget=budget,
                audit=audit,
                config={"tuning_run": run_id, "candidate": candidate_id, "parent": current.id},
                run_id=f"{run_id}-{candidate_id}",
            )
            metrics = _metrics_of(report)
            candidate = Candidate(
                id=candidate_id,
                # lineage 记在补丁上：报告里能顺着 parent_id 一路回溯到 base。
                patch=patch.model_copy(update={"parent_id": current.id}),
                metrics=metrics,
                cost_usd=report.cost_usd,
                passed=_not_worse(metrics, base_score, base_safety),
                prompt_sha=fingerprint(text),
            )
            candidates.append(candidate)
            prompts[candidate_id] = text
            results[candidate_id] = (report, verdicts)

            if audit is not None:
                audit.append(
                    "tune.candidate",
                    f"{run_id}/{candidate_id}",
                    outcome="ok" if candidate.passed else "regressed",
                    weighted_score=round(_score_of(candidate), 4),
                    safety=round(_safety_of(candidate), 4),
                    cost_usd=round(candidate.cost_usd, 6),
                )

        if exhausted:
            break
        if applied == 0:
            # 一整轮没有一份补丁能 apply，说明提议者与当前提示词已经脱节，继续跑只是烧预算。
            stopped_reason = f"第 {step + 1} 轮所有补丁都无法 apply（与当前提示词不一致）"
            break

        best = _select(candidates, base_score, base_safety)
        current = best
        if audit is not None:
            audit.append(
                "tune.select",
                f"{run_id}/{step + 1}",
                selection=best.id,
                weighted_score=round(_score_of(best), 4),
            )

    selected = _select(candidates, base_score, base_safety)
    report = TuningReport(
        run_id=run_id,
        base_run=base_report.run_id,
        objective=OBJECTIVE,
        candidates=candidates,
        pareto_front=_pareto_front(candidates),
        selected=selected.id,
        stopped_reason=stopped_reason,
        audit_ref=str(audit.path) if audit is not None else None,
    )
    if audit is not None:
        audit.append(
            "tune.end",
            run_id,
            selected=selected.id,
            stopped_reason=stopped_reason or "completed",
            candidates=len(candidates),
        )
    return report


__all__ = ["BASE_CANDIDATE_ID", "OBJECTIVE", "RUNS_ROOT", "optimize"]
