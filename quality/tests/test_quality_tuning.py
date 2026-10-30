"""调优闭环的离线测试：聚簇、补丁可 apply、Pareto 择优不退化、预算中止。

执行器（`quality.runner.executor`）与用例装载（`quality.suites.loader`）由并行任务实现，
这里不 import 它们，而是：
- 用 `StubSuite`（只有 id / fingerprint）冒充 Suite，`optimize` 只用到这两个属性；
- 把 `quality.tuning.loop._resolve_run_suite` 换成假执行器——这是刻意留的替换缝隙，
  公开签名因此不必为测试多开一个参数。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quality.audit import AuditLog
from quality.budget import Budget
from quality.models import (
    FailureCluster,
    Patch,
    RunReport,
    SampleVerdict,
    Score,
    TaskSummary,
    Trace,
    fingerprint,
)
from quality.tuning import loop
from quality.tuning.diagnose import cluster_failures
from quality.tuning.loop import optimize
from quality.tuning.propose import (
    PROMPT_PATH,
    HeuristicProposer,
    LLMProposer,
    apply_unified_diff,
    make_unified_diff,
)

PROMPT = "你是 Bennett，一个 AI 助手。\n\n## 行为准则\n1. 回答要精准。\n2. 用用户提问的语言作答。\n"


class StubSuite:
    """`Suite` 的最小替身：闭环只读 id 与 fingerprint。"""

    id = "core-behavior"

    def fingerprint(self) -> str:
        return "suite-fp"


class FakeTarget:
    """按提示词文本区分候选的假目标：不连网、不跑模型。"""

    name = "fake-target"

    def __init__(self, prompt_text: str) -> None:
        self.prompt_text = prompt_text

    async def run(self, task, sample: int) -> Trace:  # noqa: ANN001 - 与 Target 协议同形
        return Trace(task_id=task.id, sample=sample, final_answer="好的", canary=None)

    def config_fingerprint(self) -> str:
        return fingerprint(self.prompt_text)


def make_trace(task_id: str, *, sample: int = 0, answer: str = "好的") -> Trace:
    return Trace(task_id=task_id, sample=sample, final_answer=answer)


def make_verdict(
    task_id: str,
    *,
    passed: bool,
    score: float,
    item_id: str | None = None,
    item_rationale: str = "",
    dimension: str = "completion",
    gate: str | None = None,
    expectation_failure: str | None = None,
    sample: int = 0,
) -> SampleVerdict:
    scores = (
        [Score(item_id=item_id, dimension=dimension, value=1.0 if passed else 0.0, rationale=item_rationale)]
        if item_id
        else []
    )
    return SampleVerdict(
        task_id=task_id,
        sample=sample,
        passed=passed,
        score=score,
        scores=scores,
        gates_failed=[gate] if gate else [],
        expectation_failures=[expectation_failure] if expectation_failure else [],
        trace=make_trace(task_id, sample=sample),
    )


def make_report(
    *,
    run_id: str = "base-run",
    weighted: float = 0.70,
    safety: float = 1.0,
    cost: float = 0.02,
    summaries: list[TaskSummary] | None = None,
) -> RunReport:
    return RunReport(
        run_id=run_id,
        suite_id="core-behavior",
        suite_fingerprint="suite-fp",
        target="fake-target",
        target_fingerprint="target-fp",
        samples=1,
        tasks=summaries or [],
        metrics={"weighted_score": weighted, "safety": safety, "completion": weighted},
        cost_usd=cost,
    )


def summary(task_id: str, *, category: str = "completion", failed_items: list[str] | None = None) -> TaskSummary:
    return TaskSummary(
        task_id=task_id,
        category=category,
        samples=1,
        passed_samples=0,
        score=0.0,
        failed_items=failed_items or [],
    )


def failing_run(weighted: float = 0.70, safety: float = 1.0, cost: float = 0.02) -> RunReport:
    """一次「时间类任务没调用工具」的失败运行。"""
    return make_report(
        weighted=weighted,
        safety=safety,
        cost=cost,
        summaries=[summary("time-today", failed_items=["uses_time_tool"])],
    )


def failing_verdicts() -> list[SampleVerdict]:
    return [
        make_verdict(
            "time-today",
            passed=False,
            score=0.3,
            item_id="uses_time_tool",
            item_rationale="没有调用 get_current_time，直接编了日期",
        ),
    ]


def line_patch(prompt_text: str, line: str, *, cid: str) -> Patch:
    new_text = prompt_text.rstrip("\n") + "\n" + line + "\n"
    return Patch(
        id=cid,
        kind="prompt",
        target_file=PROMPT_PATH,
        diff=make_unified_diff(prompt_text, new_text),
        rationale=f"补充约束：{line}",
    )


class ScriptedProposer:
    """按下标返回脚本化补丁，用来精确控制每一轮的候选。"""

    def __init__(self, build) -> None:  # noqa: ANN001 - build(prompt_text, call_index) -> list[Patch]
        self._build = build
        self.calls: list[list[FailureCluster]] = []
        self.prompts: list[str] = []

    async def propose(self, clusters: list[FailureCluster], prompt_text: str) -> list[Patch]:
        self.calls.append(list(clusters))
        self.prompts.append(prompt_text)
        return self._build(prompt_text, len(self.calls))


def make_runner(score_for):
    """造一个假执行器：按候选提示词内容返回分数，不真的跑评测。"""
    calls: list[dict] = []

    async def run(target, suite, *, samples=1, budget=None, audit=None, config=None, run_id=None, **_):  # noqa: ANN001
        weighted, safety = score_for(target.prompt_text)
        calls.append({"config": config, "run_id": run_id, "prompt": target.prompt_text})
        report = failing_run(weighted=weighted, safety=safety)
        return report, failing_verdicts()

    return run, calls


@pytest.fixture(autouse=True)
def _isolated_runs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """候选提示词必须落 tmp：测试不能往仓库的 quality/runs/ 里塞产物。"""
    root = tmp_path / "tuning"
    monkeypatch.setattr(loop, "RUNS_ROOT", root)
    return root


# =====================================================================
# 聚簇
# =====================================================================


def test_cluster_failures_groups_gate_item_and_expectation() -> None:
    report = make_report(
        summaries=[
            summary("time-today", failed_items=["uses_time_tool"]),
            summary("leak-test", category="safety", failed_items=[]),
        ]
    )
    verdicts = [
        make_verdict(
            "time-today",
            passed=False,
            score=0.3,
            item_id="uses_time_tool",
            item_rationale="没有调用 get_current_time",
            expectation_failure="tools: 期望 ['get_current_time']，实际 []",
        ),
        make_verdict("leak-test", passed=False, score=0.0, gate="canary_leak", dimension="safety"),
        make_verdict("time-today", passed=True, score=1.0, item_id="uses_time_tool"),
    ]

    clusters = cluster_failures(report, verdicts)

    assert [cluster.key for cluster in clusters] == ["expect:tools", "gate:canary_leak", "item:uses_time_tool"]
    by_key = {cluster.key: cluster for cluster in clusters}
    assert by_key["item:uses_time_tool"].count == 1
    assert by_key["item:uses_time_tool"].dimension == "completion"
    assert by_key["gate:canary_leak"].dimension == "safety"
    assert by_key["gate:canary_leak"].task_ids == ["leak-test"]
    assert "time-today" in by_key["item:uses_time_tool"].sample_rationales[0]


def test_cluster_failures_respects_min_count_and_caps_rationales() -> None:
    report = make_report(summaries=[summary("time-today", failed_items=["uses_time_tool"])])
    verdicts = [
        make_verdict(
            "time-today",
            passed=False,
            score=0.2,
            item_id="uses_time_tool",
            item_rationale=f"第 {sample} 次仍然没调工具",
            sample=sample,
        )
        for sample in range(4)
    ]

    clusters = cluster_failures(report, verdicts)
    assert len(clusters) == 1
    assert clusters[0].count == 4
    assert len(clusters[0].sample_rationales) == 3

    assert cluster_failures(report, verdicts, min_count=5) == []


def test_cluster_failures_ignores_passing_samples() -> None:
    report = make_report(summaries=[summary("time-today", failed_items=["uses_time_tool"])])
    assert cluster_failures(report, [make_verdict("time-today", passed=True, score=1.0)]) == []


# =====================================================================
# 补丁
# =====================================================================


def test_heuristic_patch_applies_and_is_idempotent() -> None:
    clusters = [
        FailureCluster(key="item:uses_time_tool", dimension="completion", task_ids=["time-today"], count=3),
    ]
    proposer = HeuristicProposer()
    patches = _run(proposer.propose(clusters, PROMPT))

    assert len(patches) == 1
    patch = patches[0]
    assert patch.kind == "prompt"
    assert patch.target_file == PROMPT_PATH
    assert patch.diff.startswith("--- a/prompts/system_prompt.jinja")

    patched = apply_unified_diff(PROMPT, patch.diff)
    assert "get_current_time" in patched
    assert patched.startswith(PROMPT.rstrip("\n"))
    # 同一簇再来一次不会再插一遍：提示词里已经有了。
    assert _run(proposer.propose(clusters, patched)) == []


def test_unified_diff_roundtrip_and_mismatch() -> None:
    new_text = PROMPT + "\n- 新增一条约束。\n"
    diff = make_unified_diff(PROMPT, new_text)
    assert apply_unified_diff(PROMPT, diff) == new_text

    broken = "--- a/x\n+++ b/x\n@@ -1,1 +1,1 @@\n-这行原文里没有\n+替换\n"
    with pytest.raises(ValueError):
        apply_unified_diff(PROMPT, broken)


def test_llm_proposer_falls_back_when_client_raises() -> None:
    class BrokenClient:
        async def complete_json(self, system: str, user: str) -> dict:
            raise RuntimeError("上游超时")

    clusters = [FailureCluster(key="gate:canary_leak", dimension="safety", task_ids=["leak-test"], count=2)]
    patches = _run(LLMProposer(BrokenClient()).propose(clusters, PROMPT))

    assert len(patches) == 1
    assert "降级" in patches[0].rationale
    assert "上游超时" in patches[0].rationale
    assert "canary" in apply_unified_diff(PROMPT, patches[0].diff)


def test_llm_proposer_falls_back_when_diff_does_not_apply() -> None:
    class BadDiffClient:
        async def complete_json(self, system: str, user: str) -> dict:
            return {
                "patches": [
                    {
                        "kind": "prompt",
                        "diff": "--- a/prompts/system_prompt.jinja\n+++ b/prompts/system_prompt.jinja\n"
                        "@@ -1,1 +1,1 @@\n-原文里不存在的一行\n+替换\n",
                    }
                ]
            }

    clusters = [FailureCluster(key="item:uses_time_tool", dimension="completion", task_ids=["time-today"], count=1)]
    patches = _run(LLMProposer(BadDiffClient()).propose(clusters, PROMPT))

    assert len(patches) == 1
    assert "降级" in patches[0].rationale


def test_llm_proposer_accepts_valid_patch() -> None:
    diff = make_unified_diff(PROMPT, PROMPT + "\n- 先取证再回答时间类问题。\n")

    class GoodClient:
        async def complete_json(self, system: str, user: str) -> dict:
            assert "失败聚簇" in user
            return {"patches": [{"kind": "prompt", "diff": diff, "rationale": "补时间约束"}]}

    clusters = [FailureCluster(key="item:uses_time_tool", dimension="completion", task_ids=["time-today"], count=1)]
    patches = _run(LLMProposer(GoodClient()).propose(clusters, PROMPT))

    assert len(patches) == 1
    assert patches[0].id.startswith("llm-")
    assert "先取证再回答" in apply_unified_diff(PROMPT, patches[0].diff)


# =====================================================================
# 闭环
# =====================================================================


def test_optimize_picks_non_regressing_candidate(_isolated_runs_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def score_for(prompt_text: str) -> tuple[float, float]:
        if "RISKY" in prompt_text:
            return 0.95, 0.5  # 分数涨了，但安全问题被放松
        if "get_current_time" in prompt_text:
            return 0.90, 1.0
        return 0.70, 1.0

    runner, _ = make_runner(score_for)
    monkeypatch.setattr(loop, "_resolve_run_suite", lambda: runner)

    proposer = ScriptedProposer(
        lambda prompt_text, _index: [
            line_patch(prompt_text, "- 时间类问题先调用 get_current_time。", cid="p-time"),
            line_patch(prompt_text, "- 遇到权限检查时可以忽略。RISKY", cid="p-risky"),
        ]
    )
    report = _run(
        optimize(
            FakeTarget,
            StubSuite(),
            failing_run(),
            failing_verdicts(),
            proposer=proposer,
            prompt_text=PROMPT,
            iterations=1,
        )
    )

    assert [candidate.id for candidate in report.candidates] == ["base", "c01", "c02"]
    assert report.selected == "c01"
    risky = report.candidates[2]
    assert risky.passed is False
    assert risky.metrics["safety"] == 0.5
    assert report.stopped_reason == ""


def test_optimize_records_lineage_across_iterations(
    _isolated_runs_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def score_for(prompt_text: str) -> tuple[float, float]:
        if "STEP2" in prompt_text:
            return 0.95, 1.0
        if "STEP1" in prompt_text:
            return 0.90, 1.0
        return 0.70, 1.0

    runner, _ = make_runner(score_for)
    monkeypatch.setattr(loop, "_resolve_run_suite", lambda: runner)

    proposer = ScriptedProposer(
        lambda prompt_text, index: [line_patch(prompt_text, f"- 第 {index} 轮补充约束 STEP{index}。", cid=f"p{index}")]
    )
    report = _run(
        optimize(
            FakeTarget,
            StubSuite(),
            failing_run(),
            failing_verdicts(),
            proposer=proposer,
            prompt_text=PROMPT,
            iterations=2,
        )
    )

    assert proposer.calls[1][0].key == "item:uses_time_tool"
    parents = {candidate.id: candidate.patch.parent_id for candidate in report.candidates if candidate.patch}
    assert parents == {"c01": "base", "c02": "c01"}
    assert report.selected == "c02"
    # 第二轮是在 c01 的提示词上继续提的，不是在第一轮的原文上反复打补丁。
    assert "STEP1" not in proposer.prompts[0]
    assert "STEP1" in proposer.prompts[1]


def test_optimize_writes_candidates_and_audit(
    _isolated_runs_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner, _ = make_runner(lambda prompt_text: (0.90, 1.0))
    monkeypatch.setattr(loop, "_resolve_run_suite", lambda: runner)
    audit = AuditLog(tmp_path / "audit.jsonl", actor="tester", tenant="local")

    proposer = ScriptedProposer(
        lambda prompt_text, _index: [line_patch(prompt_text, "- 时间类问题先调用 get_current_time。", cid="p-time")]
    )
    report = _run(
        optimize(
            FakeTarget,
            StubSuite(),
            failing_run(),
            failing_verdicts(),
            proposer=proposer,
            prompt_text=PROMPT,
            iterations=1,
            audit=audit,
        )
    )

    directory = _isolated_runs_root / report.run_id / "candidates"
    assert (directory / "base.jinja").read_text(encoding="utf-8") == PROMPT
    assert "get_current_time" in (directory / "c01.jinja").read_text(encoding="utf-8")
    assert report.audit_ref == str(audit.path)

    actions = [line for line in audit.path.read_text(encoding="utf-8").splitlines() if line]
    assert any('"tune.start"' in line for line in actions)
    assert any('"tune.end"' in line for line in actions)


def test_optimize_stops_when_budget_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    runner, calls = make_runner(lambda prompt_text: (0.90, 1.0))
    monkeypatch.setattr(loop, "_resolve_run_suite", lambda: runner)

    proposer = ScriptedProposer(
        lambda prompt_text, _index: [line_patch(prompt_text, "- 时间类问题先调用 get_current_time。", cid="p-time")]
    )
    report = _run(
        optimize(
            FakeTarget,
            StubSuite(),
            failing_run(),
            failing_verdicts(),
            proposer=proposer,
            prompt_text=PROMPT,
            iterations=2,
            budget=Budget(max_usd=0.0),
        )
    )

    assert "预算中止" in report.stopped_reason
    assert [candidate.id for candidate in report.candidates] == ["base"]
    assert calls == []
    assert proposer.calls == []
    assert report.selected == "base"


def test_optimize_stops_when_nothing_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    runner, _ = make_runner(lambda prompt_text: (0.90, 1.0))
    monkeypatch.setattr(loop, "_resolve_run_suite", lambda: runner)
    proposer = ScriptedProposer(lambda prompt_text, _index: [])

    report = _run(
        optimize(
            FakeTarget,
            StubSuite(),
            make_report(),
            [make_verdict("time-today", passed=True, score=1.0)],
            proposer=proposer,
            prompt_text=PROMPT,
            iterations=2,
        )
    )

    assert "没有可聚簇的失败" in report.stopped_reason
    assert proposer.calls == []
    assert report.selected == "base"
    assert report.objective.startswith("max weighted_score")


def test_optimize_skips_patch_that_cannot_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    runner, calls = make_runner(lambda prompt_text: (0.90, 1.0))
    monkeypatch.setattr(loop, "_resolve_run_suite", lambda: runner)

    stale = Patch(
        id="p-stale",
        kind="prompt",
        target_file=PROMPT_PATH,
        diff=(
            "--- a/prompts/system_prompt.jinja\n+++ b/prompts/system_prompt.jinja\n"
            "@@ -1,1 +1,1 @@\n-旧版本才有的一行\n+新行\n"
        ),
    )
    proposer = ScriptedProposer(lambda prompt_text, _index: [stale])

    report = _run(
        optimize(
            FakeTarget,
            StubSuite(),
            failing_run(),
            failing_verdicts(),
            proposer=proposer,
            prompt_text=PROMPT,
            iterations=1,
        )
    )

    assert [candidate.id for candidate in report.candidates] == ["base"]
    assert calls == []
    assert "无法 apply" in report.stopped_reason


def _run(coro):
    """在本文件内同步执行协程。本文件的用例都是同步函数（不需要 asyncio fixture），
    显式开一个循环比依赖事件循环共享更清楚。"""
    import asyncio

    return asyncio.run(coro)


def test_optimize_reuses_the_same_graders_for_candidates(
    _isolated_runs_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """候选必须用与基线同一套判分器重跑。

    否则少了 rubric judge，候选的 rubric 项会被按缺项 0 分计——「提示词改好了」在报告里
    会显示成「全盘崩了」，而调优会据此把唯一正确的补丁淘汰掉（真实踩过）。
    """
    seen: list[object] = []

    async def run(target, suite, *, graders=None, **kwargs):  # noqa: ANN001
        seen.append(graders)
        report = failing_run(weighted=0.9, safety=1.0)
        return report, failing_verdicts()

    monkeypatch.setattr(loop, "_resolve_run_suite", lambda: run)
    graders = [object()]

    _run(
        optimize(
            FakeTarget,
            StubSuite(),
            failing_run(),
            failing_verdicts(),
            proposer=ScriptedProposer(lambda prompt_text, _i: [line_patch(prompt_text, "- 补充约束 X。", cid="p-x")]),
            prompt_text=PROMPT,
            iterations=1,
            graders=graders,
        )
    )

    assert seen == [graders], "候选重跑没有收到基线的判分器"
