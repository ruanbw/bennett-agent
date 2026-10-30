"""执行器 + 指标 + 落盘的测试：全部用假 Target / 假 Grader，不连网、不 import app。

为什么要假 Target 而不是跑真实的 PromptLabTarget：这里要验证的是**执行器的机制**
（并发不丢样本、单样本失败降级、预算耗尽标记 partial、产物可复现），跟模型说了什么
无关。真跑模型会让这些用例变慢、变贵、并且随机红——机制测试一旦随机红，就没人看了。
判分口径的测试在 `tests/test_quality_graders.py` 里。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from quality.audit import AuditLog
from quality.budget import Budget
from quality.models import Gate, RubricItem, SampleVerdict, Score, Task, Trace
from quality.runner.executor import EVAL_ERROR_PREFIX, run_suite
from quality.runner.metrics import compute_metrics, summarise
from quality.runner.report import render_html, write_json
from quality.suites.loader import Suite

# =====================================================================
# 测试替身
# =====================================================================


class FakeTarget:
    """按 `task.setup["behaviour"]` 决定这次样本怎么结束，按需记录并发峰值。"""

    name = "fake"

    def __init__(self, *, cost_usd: float = 0.0, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.cost_usd = cost_usd
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.calls: list[tuple[str, int]] = []
        self.peak = 0
        self._active = 0

    def config_fingerprint(self) -> str:
        return "fake-target-v1"

    async def run(self, task: Task, sample: int) -> Trace:
        self._active += 1
        self.peak = max(self.peak, self._active)
        try:
            self.calls.append((task.id, sample))
            # 让出事件循环：并发用例需要真的交错，否则 Semaphore 永远不会被同时争用。
            await asyncio.sleep(0)
            behaviour = task.setup.get("behaviour", "answer")
            if behaviour == "raise":
                raise RuntimeError("upstream 500")
            if behaviour == "not-a-trace":
                return {"task_id": task.id}  # type: ignore[return-value]
            return Trace(
                task_id=task.id,
                sample=sample,
                final_answer=f"{task.id}#{sample} 的回答",
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                cost_usd=self.cost_usd,
                config_fingerprint=self.config_fingerprint(),
            )
        finally:
            self._active -= 1


class StaticGrader:
    """按任务 id 给固定分数。没有它就没法在不接 LLM 的情况下测 `judge` 的接线。"""

    id = "static"

    def __init__(self, values: dict[str, float] | None = None, *, default: float = 1.0, error: Exception | None = None):
        self.values = values or {}
        self.default = default
        self.error = error

    async def grade(self, task: Task, trace: Trace) -> list[Score]:
        if self.error is not None:
            raise self.error
        value = self.values.get(task.id, self.default)
        return [
            Score(item_id=item.id, dimension=item.dimension, value=value, rationale="static", source="deterministic")
            for item in task.rubric
        ]


def make_task(
    task_id: str = "task-one",
    *,
    category: str = "completion",
    rubric: bool = True,
    gates: list[Gate] | None = None,
    expect: dict | None = None,
    setup: dict | None = None,
    pass_threshold: float = 0.75,
) -> Task:
    return Task(
        id=task_id,
        category=category,  # type: ignore[arg-type]
        prompt="问题",
        rubric=([RubricItem(id="did_it", dimension=category, description="做到这件事")] if rubric else []),  # type: ignore[arg-type]
        gates=gates or [],
        expect=expect,  # type: ignore[arg-type]
        setup=setup or {},
        pass_threshold=pass_threshold,
    )


def make_suite(*tasks: Task, suite_id: str = "core-behavior") -> Suite:
    return Suite(id=suite_id, version=1, description="测试用套件", tasks=list(tasks))


def make_verdict(
    task_id: str,
    sample: int,
    *,
    passed: bool = True,
    score: float = 1.0,
    scores: list[Score] | None = None,
    gates: list[str] | None = None,
    expectations: list[str] | None = None,
) -> SampleVerdict:
    return SampleVerdict(
        task_id=task_id,
        sample=sample,
        passed=passed,
        score=score,
        scores=list(scores or []),
        gates_failed=list(gates or []),
        expectation_failures=list(expectations or []),
        trace=Trace(task_id=task_id, sample=sample),
    )


# =====================================================================
# 执行器
# =====================================================================


async def test_run_suite_all_pass_records_fingerprints_and_totals():
    suite = make_suite(make_task("task-one"), make_task("task-two"))
    target = FakeTarget(cost_usd=0.5, input_tokens=10, output_tokens=5)

    report, verdicts = await run_suite(
        target, suite, samples=2, concurrency=2, graders=[StaticGrader()], run_id="run-all-pass"
    )

    assert len(verdicts) == 4
    assert all(verdict.passed for verdict in verdicts)
    assert report.run_id == "run-all-pass"
    # 指纹进报告是复现的前提：没有它，两份报告的差异无法归因。
    assert report.suite_fingerprint == suite.fingerprint()
    assert report.target_fingerprint == "fake-target-v1"
    assert report.target == "fake"
    assert report.metrics["completion"] == 1.0
    assert report.metrics["pass_pow_k"] == 1.0
    assert report.metrics["pass_at_k"] == 1.0
    assert [summary.task_id for summary in report.tasks] == ["task-one", "task-two"]
    assert report.totals["verdicts"] == 4.0
    assert report.totals["passed"] == 4.0
    assert report.totals["tokens"] == 60.0
    assert report.cost_usd == pytest.approx(2.0)
    assert report.config["samples"] == 2
    assert report.config["partial"] is False


async def test_partial_failure_shows_up_in_metrics_and_failed_items():
    suite = make_suite(make_task("task-good"), make_task("task-bad"))
    graders = [StaticGrader({"task-bad": 0.2})]

    report, _verdicts = await run_suite(FakeTarget(), suite, samples=2, graders=graders, run_id="run-mixed")

    bad = report.task("task-bad")
    assert bad is not None
    assert (bad.passed_samples, bad.samples) == (0, 2)
    assert bad.failed_items == ["did_it"]
    # 一条任务两次全挂 → pass^k 记 0.5（两条任务里只有一条稳定通过）；
    # 但 pass@k 仍为 0.5，因为「做到过」和「做得稳」是两件事，报告要能分开读。
    assert report.metrics["pass_at_k"] == 0.5
    assert report.metrics["pass_pow_k"] == 0.5
    assert report.metrics["completion"] == 0.5


async def test_target_exception_becomes_failed_verdict_without_aborting_run():
    suite = make_suite(
        make_task("task-boom", expect={"answer_contains": ["回答"]}, setup={"behaviour": "raise"}),
        make_task("task-ok"),
    )

    report, verdicts = await run_suite(FakeTarget(), suite, samples=1, graders=[StaticGrader()], run_id="run-crash")

    # 整轮没崩：两条任务都产出了结论，另一条不受影响。
    assert len(verdicts) == 2
    crashed = next(verdict for verdict in verdicts if verdict.task_id == "task-boom")
    assert crashed.trace.error is not None and "upstream 500" in crashed.trace.error
    # 有 expect 的任务里，执行失败由 harness 记成 `error:` 断言失败——所以判分口径由
    # graders 决定，执行器不重新发明一套（见 graders/base.py 的 judge 注释）。
    assert crashed.passed is False
    assert crashed.expectation_failures == [f"error: {crashed.trace.error}"]
    assert report.task("task-ok").passed_samples == 1  # type: ignore[union-attr]


async def test_target_returning_non_trace_is_recorded_as_error():
    suite = make_suite(make_task("task-weird", setup={"behaviour": "not-a-trace"}))

    _report, verdicts = await run_suite(FakeTarget(), suite, samples=1, graders=[StaticGrader()], run_id="run-weird")

    assert len(verdicts) == 1
    assert verdicts[0].trace.error is not None
    assert "不是 Trace" in verdicts[0].trace.error


async def test_grader_crash_degrades_to_not_passed_verdict():
    suite = make_suite(make_task("task-one"))

    _report, verdicts = await run_suite(
        FakeTarget(),
        suite,
        samples=1,
        graders=[StaticGrader(error=RuntimeError("判分器 bug"))],
        run_id="run-grader-bug",
    )

    assert len(verdicts) == 1
    assert verdicts[0].passed is False
    assert verdicts[0].expectation_failures == [f"{EVAL_ERROR_PREFIX}grader:static: RuntimeError: 判分器 bug"]


async def test_gate_detector_crash_is_not_read_as_safe():
    suite = make_suite(
        make_task(
            "task-gated",
            gates=[Gate(id="no_leak", detector="detector_that_does_not_exist", severity="critical")],
        )
    )

    _report, verdicts = await run_suite(FakeTarget(), suite, samples=1, graders=[StaticGrader()], run_id="run-gate-bug")

    assert len(verdicts) == 1
    assert verdicts[0].passed is False
    assert verdicts[0].expectation_failures[0].startswith(f"{EVAL_ERROR_PREFIX}gate:")


async def test_budget_exhaustion_stops_dispatch_and_marks_partial():
    suite = make_suite(make_task("task-one"), make_task("task-two"), make_task("task-three"))
    budget = Budget(max_usd=0.5)

    report, verdicts = await run_suite(
        FakeTarget(cost_usd=1.0),
        suite,
        samples=1,
        concurrency=1,  # 串行才能断言「只跑了第一条」
        graders=[StaticGrader()],
        budget=budget,
        run_id="run-budget",
    )

    assert len(verdicts) == 1
    assert budget.usd_used == pytest.approx(1.0)  # 每次样本都记账
    assert report.config["partial"] is True
    assert "预算耗尽" in report.config["stopped_reason"]
    assert report.config["skipped_samples"] == 2
    # 没跑成的任务不进任务表：把「没跑」记成 0 分会伪造回归。
    assert [summary.task_id for summary in report.tasks] == ["task-one"]


async def test_budget_exhausted_before_first_dispatch_runs_nothing():
    suite = make_suite(make_task("task-one"))

    report, verdicts = await run_suite(
        FakeTarget(),
        suite,
        samples=1,
        graders=[StaticGrader()],
        budget=Budget(max_usd=0.0),
        run_id="run-no-budget",
    )

    assert verdicts == []
    assert report.tasks == []
    assert report.config["partial"] is True
    assert report.config["skipped_samples"] == 1
    # 一条都没跑时指标记 0：空报告不能显示成满分。
    assert report.metrics["weighted_score"] == 0.0


async def test_concurrency_is_bounded_and_no_sample_is_lost():
    suite = make_suite(*[make_task(f"task-{index}") for index in range(5)])
    target = FakeTarget()

    _report, verdicts = await run_suite(
        target, suite, samples=3, concurrency=2, graders=[StaticGrader()], run_id="run-concurrency"
    )

    assert len(verdicts) == 15
    assert {(verdict.task_id, verdict.sample) for verdict in verdicts} == {
        (f"task-{index}", sample) for index in range(5) for sample in range(3)
    }
    assert target.peak <= 2
    # 落盘顺序按 (任务序, 样本号) 还原，与完成顺序无关：同样的输入必须产出同样的文件。
    assert [(verdict.task_id, verdict.sample) for verdict in verdicts] == [
        (f"task-{index}", sample) for index in range(5) for sample in range(3)
    ]


async def test_audit_records_run_start_and_finish(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl", actor="tester", tenant="quality")
    suite = make_suite(make_task("task-one"))

    report, _verdicts = await run_suite(
        FakeTarget(), suite, samples=1, graders=[StaticGrader()], audit=audit, run_id="run-audit"
    )

    entries = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [entry["action"] for entry in entries] == ["run.start", "run.finish"]
    assert entries[0]["detail"]["suite_fingerprint"] == suite.fingerprint()
    assert entries[1]["outcome"] == "ok"
    assert report.audit_ref == str(audit.path)


async def test_default_run_id_is_utc_timestamp():
    suite = make_suite(make_task("task-one"))

    report, _verdicts = await run_suite(FakeTarget(), suite, samples=1, graders=[StaticGrader()])

    assert len(report.run_id) == 16 and report.run_id.endswith("Z")


async def test_invalid_samples_and_concurrency_are_rejected():
    suite = make_suite(make_task("task-one"))
    with pytest.raises(ValueError):
        await run_suite(FakeTarget(), suite, samples=0, graders=[StaticGrader()])
    with pytest.raises(ValueError):
        await run_suite(FakeTarget(), suite, samples=1, concurrency=0, graders=[StaticGrader()])


# =====================================================================
# 指标
# =====================================================================


def test_summarise_weights_dimension_scores_by_rubric_weight():
    task = Task(
        id="task-one",
        category="completion",
        prompt="问题",
        rubric=[
            RubricItem(id="heavy", dimension="completion", description="重要项", weight=3),
            RubricItem(id="light", dimension="completion", description="次要项", weight=1),
        ],
    )
    verdicts = [
        make_verdict(
            task.id,
            0,
            scores=[
                Score(item_id="heavy", dimension="completion", value=1.0),
                # 缺项按 0 计：判分器没给这一项结论，不能白拿分。
            ],
        )
    ]

    summary = summarise(task, verdicts)

    assert summary.dimension_scores["completion"] == round(3 * 1.0 / 4, 6)
    assert summary.failed_items == ["light"]


def test_summarise_counts_gate_hits_and_cost():
    task = make_task(
        "task-gated",
        category="safety",
        gates=[Gate(id="no_leak", detector="canary_leak", severity="critical")],
        pass_threshold=1.0,
    )
    verdicts = [
        make_verdict(task.id, 0, passed=False, gates=["no_leak"]),
        make_verdict(task.id, 1),
    ]
    verdicts[0].trace.cost_usd = 0.25

    summary = summarise(task, verdicts)

    assert summary.gates_failed == ["no_leak"]
    assert summary.passed_samples == 1
    assert summary.cost_usd == pytest.approx(0.25)


def test_compute_metrics_pass_pow_k_requires_every_sample_to_pass():
    task = make_task("task-flaky")
    # 3 次里过 2 次：pass@k 通过，pass^k 不通过（单次侥幸不算稳）。
    verdicts = [
        make_verdict("task-flaky", 0),
        make_verdict("task-flaky", 1),
        make_verdict("task-flaky", 2, passed=False, score=0.0),
    ]
    summaries = [summarise(task, verdicts)]

    metrics = compute_metrics(summaries, 3, verdicts=verdicts)

    assert metrics["pass_at_k"] == 1.0
    assert metrics["pass_pow_k"] == 0.0
    assert metrics["completion"] == round(2 / 3, 6)


def test_compute_metrics_robustness_uses_all_k_passes():
    stable = make_task("task-stable", category="robustness")
    flaky = make_task("task-flaky", category="robustness")
    verdicts = [
        make_verdict("task-stable", 0),
        make_verdict("task-stable", 1),
        make_verdict("task-flaky", 0),
        make_verdict("task-flaky", 1, passed=False, score=0.0),
    ]
    summaries = [summarise(task, verdicts) for task in (stable, flaky)]

    metrics = compute_metrics(summaries, 2, verdicts=verdicts)

    assert metrics["robustness"] == 0.5


def test_compute_metrics_safety_counts_gate_hits_per_sample():
    task = make_task(
        "task-gated",
        category="safety",
        gates=[Gate(id="no_leak", detector="canary_leak", severity="critical")],
        pass_threshold=1.0,
    )
    verdicts = [
        make_verdict("task-gated", 0, passed=False, gates=["no_leak"]),
        make_verdict("task-gated", 1),
        make_verdict("task-gated", 2),
    ]
    summaries = [summarise(task, verdicts)]

    metrics = compute_metrics(summaries, 3, verdicts=verdicts)

    assert metrics["safety"] == round(1 - 1 / 3, 6)


def test_compute_metrics_safety_without_verdicts_falls_back_to_task_level():
    task = make_task(
        "task-gated",
        category="safety",
        gates=[Gate(id="no_leak", detector="canary_leak", severity="critical")],
        pass_threshold=1.0,
    )
    summaries = [summarise(task, [make_verdict("task-gated", 0, passed=False, gates=["no_leak"])])]

    metrics = compute_metrics(summaries, 1)

    assert metrics["safety"] == 0.0


def test_compute_metrics_efficiency_is_one_without_budget_and_tracks_usage_with_it():
    task = make_task("task-one")
    summaries = [summarise(task, [make_verdict("task-one", 0)])]

    assert compute_metrics(summaries, 1)["efficiency"] == 1.0

    budget = Budget(max_usd=2.0, max_tokens=100)
    budget.charge(tokens=10, usd=0.5)
    # 取最紧的一项：美元用了 25%，token 用了 10% → 25% 才是最接近红线的那项。
    assert compute_metrics(summaries, 1, budget=budget)["efficiency"] == pytest.approx(0.75)


def test_compute_metrics_defaults_to_pass_for_absent_categories():
    task = make_task("task-one")
    summaries = [summarise(task, [make_verdict("task-one", 0)])]

    metrics = compute_metrics(summaries, 1)

    # 没有安全任务时 safety 不该被算成 0：那会让只跑核心套件的运行永远红。
    assert metrics["safety"] == 1.0
    assert metrics["robustness"] == 1.0
    assert set(metrics) == {
        "completion",
        "robustness",
        "safety",
        "efficiency",
        "pass_pow_k",
        "pass_at_k",
        "weighted_score",
    }


def test_compute_metrics_without_any_task_does_not_fake_a_pass():
    metrics = compute_metrics([], 3)

    # 空集合「恒为真」在报告里会显示成满分，那是最坏的一种失真。
    assert metrics["weighted_score"] == 0.0
    assert metrics["completion"] == 0.0
    assert metrics["pass_at_k"] == 0.0
    assert metrics["efficiency"] == 1.0


# =====================================================================
# 落盘
# =====================================================================


async def test_write_json_writes_report_and_sorted_verdicts(tmp_path):
    suite = make_suite(make_task("task-one"), make_task("task-two"))
    report, verdicts = await run_suite(
        FakeTarget(), suite, samples=2, concurrency=2, graders=[StaticGrader()], run_id="run-write"
    )

    directory = write_json(report, list(reversed(verdicts)), tmp_path / "run-write")

    assert directory == tmp_path / "run-write"
    payload = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    assert payload["run_id"] == "run-write"
    assert payload["metrics"]["completion"] == 1.0
    lines = (directory / "verdicts.jsonl").read_text(encoding="utf-8").splitlines()
    assert [(json.loads(line)["task_id"], json.loads(line)["sample"]) for line in lines] == [
        ("task-one", 0),
        ("task-one", 1),
        ("task-two", 0),
        ("task-two", 1),
    ]


async def test_render_html_writes_single_file(tmp_path):
    suite = make_suite(make_task("task-one"))
    report, verdicts = await run_suite(FakeTarget(), suite, samples=1, graders=[StaticGrader()], run_id="run-html")

    path = render_html(report, verdicts, tmp_path / "report.html")

    assert path.exists()
    assert "run-html" in path.read_text(encoding="utf-8")
