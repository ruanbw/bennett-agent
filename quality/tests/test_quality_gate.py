"""门禁测试：只构造两份 `RunReport`，不跑执行器也不连网。

门禁是「能不能发版」的唯一自动判据，所以每个判定分支都要单测到：指标跌幅、安全跌幅、
suite 指纹不一致、critical gate、以及「跑不完不放行」。失败时不给出原因的门禁，
在 CI 里只会被人 `--skip` 掉。
"""

from __future__ import annotations

from typing import Any

import pytest

from quality.models import RunReport, TaskSummary
from quality.runner.gate import GATE_METRICS, evaluate_gate

FULL_METRICS: dict[str, float] = {
    "completion": 1.0,
    "robustness": 1.0,
    "safety": 1.0,
    "efficiency": 1.0,
    "pass_pow_k": 1.0,
    "pass_at_k": 1.0,
    "weighted_score": 1.0,
}


def make_report(
    *,
    run_id: str = "run",
    metrics: dict[str, float] | None = None,
    tasks: list[TaskSummary] | None = None,
    config: dict[str, Any] | None = None,
    suite_fingerprint: str = "suite-0001",
    target_fingerprint: str = "target-0001",
    samples: int = 1,
) -> RunReport:
    return RunReport(
        run_id=run_id,
        suite_id="core-behavior",
        suite_fingerprint=suite_fingerprint,
        target="fake",
        target_fingerprint=target_fingerprint,
        config=dict(config or {}),
        samples=samples,
        tasks=list(tasks or []),
        metrics=dict(FULL_METRICS if metrics is None else metrics),
    )


def with_metrics(**changes: float) -> dict[str, float]:
    payload = dict(FULL_METRICS)
    payload.update(changes)
    return payload


def summary(task_id: str = "task-gated", *, category: str = "safety", gates: list[str] | None = None) -> TaskSummary:
    return TaskSummary(
        task_id=task_id,
        category=category,  # type: ignore[arg-type]
        samples=1,
        passed_samples=0 if gates else 1,
        score=1.0 if not gates else 0.0,
        gates_failed=list(gates or []),
    )


def test_identical_reports_pass_with_zero_deltas():
    baseline = make_report(run_id="base")

    outcome = evaluate_gate(make_report(run_id="cur"), baseline)

    assert outcome.failed is False
    assert outcome.reasons == []
    assert set(outcome.deltas) == set(GATE_METRICS)
    assert all(delta == 0.0 for delta in outcome.deltas.values())


def test_completion_drop_beyond_threshold_fails_with_reason():
    baseline = make_report(run_id="base")
    current = make_report(run_id="cur", metrics=with_metrics(completion=0.90))

    outcome = evaluate_gate(current, baseline)

    assert outcome.failed is True
    assert outcome.deltas["completion"] == pytest.approx(-0.10)
    assert any("completion" in reason for reason in outcome.failures)


def test_small_drop_within_threshold_passes():
    baseline = make_report(run_id="base")
    current = make_report(run_id="cur", metrics=with_metrics(weighted_score=0.985))

    # 0.015 的回落小于默认允许的 0.02：噪声不该拦发布，否则门禁会被当噪音关掉。
    outcome = evaluate_gate(current, baseline)

    assert outcome.failed is False
    assert outcome.deltas["weighted_score"] == pytest.approx(-0.015)


def test_any_safety_drop_fails_by_default():
    baseline = make_report(run_id="base")
    current = make_report(run_id="cur", metrics=with_metrics(safety=0.99))

    outcome = evaluate_gate(current, baseline)

    assert outcome.failed is True
    assert any("safety" in reason for reason in outcome.failures)


def test_safety_tolerance_can_be_relaxed_explicitly():
    baseline = make_report(run_id="base")
    current = make_report(run_id="cur", metrics=with_metrics(safety=0.99))

    outcome = evaluate_gate(current, baseline, max_safety_regression=0.05)

    assert outcome.failed is False
    assert outcome.deltas["safety"] == pytest.approx(-0.01)


def test_suite_fingerprint_mismatch_refuses_to_compare():
    baseline = make_report(run_id="base", suite_fingerprint="suite-aaaa")

    outcome = evaluate_gate(make_report(run_id="cur", suite_fingerprint="suite-bbbb"), baseline)

    # 换考卷就是换了分母，结论无法归因到被测系统；静默对比会把真实回归藏起来。
    assert outcome.failed is True
    assert any("suite 指纹" in reason for reason in outcome.failures)


def test_target_fingerprint_mismatch_is_only_a_note():
    baseline = make_report(run_id="base", target_fingerprint="target-aaaa")

    outcome = evaluate_gate(make_report(run_id="cur", target_fingerprint="target-bbbb"), baseline)

    # 换被测系统正是评测的日常用法（改了提示词跑一遍），不该拦。
    assert outcome.failed is False
    assert outcome.failures == []
    assert len(outcome.notes) == 1
    assert "目标配置指纹" in outcome.notes[0]


def test_critical_gate_failure_blocks_release():
    current = make_report(
        run_id="cur",
        tasks=[summary(gates=["no_leak"])],
        config={"gate_severities": {"no_leak": "critical"}},
    )

    outcome = evaluate_gate(current, make_report(run_id="base"))

    assert outcome.failed is True
    assert any("critical gate" in reason for reason in outcome.failures)


def test_non_critical_gate_failure_does_not_block_by_itself():
    current = make_report(
        run_id="cur",
        tasks=[summary(gates=["no_leak"])],
        config={"gate_severities": {"no_leak": "high"}},
    )

    outcome = evaluate_gate(current, make_report(run_id="base"))

    # 高分严重级的 gate 命中会通过 pass 率把指标拉下去，由跌幅规则负责拦；
    # 这里只确认「不是 critical 就不额外判死」，否则所有带 gate 的套件都会常红。
    assert outcome.failed is False


def test_missing_gate_severities_is_reported_as_note():
    current = make_report(run_id="cur", tasks=[summary(gates=["no_leak"])])

    outcome = evaluate_gate(current, make_report(run_id="base"))

    assert outcome.failed is False
    assert any("严重级" in note for note in outcome.notes)


def test_partial_run_is_never_released():
    current = make_report(run_id="cur", config={"partial": True, "stopped_reason": "token 预算耗尽（10/10）"})

    outcome = evaluate_gate(current, make_report(run_id="base"))

    # 样本不全时指标分母变小，常常比基线还好看——这正是最容易被误读成「改进」的情况。
    assert outcome.failed is True
    assert any("partial" in reason and "token 预算耗尽" in reason for reason in outcome.failures)


def test_samples_mismatch_refuses_to_compare():
    baseline = make_report(run_id="base", samples=3)

    outcome = evaluate_gate(make_report(run_id="cur", samples=1), baseline)

    # pass^k 的分母变了就不是同一个数：k 变小会把真实回归抹匀，k 变大会把噪声读成回归。
    assert outcome.failed is True
    assert any("采样数" in reason for reason in outcome.failures)


def test_judge_model_mismatch_refuses_to_compare():
    baseline = make_report(run_id="base", config={"judge_model": "group/judge-a"})

    outcome = evaluate_gate(make_report(run_id="cur", config={"judge_model": "group/judge-b"}), baseline)

    # 分数出自不同的判分器（self-preference 有实证），差异无法归因到被测系统。
    assert outcome.failed is True
    assert any("判分模型" in reason for reason in outcome.failures)


def test_same_judge_model_passes():
    config = {"judge_model": "group/judge-a"}

    outcome = evaluate_gate(make_report(run_id="cur", config=config), make_report(run_id="base", config=config))

    assert outcome.failed is False


def test_missing_judge_model_on_both_sides_is_not_a_mismatch():
    # 历史报告（加了 judge_model 之前跑出来的）不能被一次性判红，否则所有基线都得重跑。
    outcome = evaluate_gate(make_report(run_id="cur"), make_report(run_id="base"))

    assert outcome.failed is False


def test_judge_errors_block_release():
    current = make_report(run_id="cur", config={"judge_model": "group/judge-a", "judge_errors": 2})
    baseline = make_report(run_id="base", config={"judge_model": "group/judge-a", "judge_errors": 0})

    outcome = evaluate_gate(current, baseline)

    # 与 partial 同口径：那些 0 分来自判分器故障，指标不是被测系统的分数。
    assert outcome.failed is True
    assert any("判分调用失败" in reason for reason in outcome.failures)


def test_metric_missing_on_one_side_is_noted_not_failed():
    baseline = make_report(run_id="base")
    current_metrics = with_metrics()
    current_metrics.pop("efficiency")

    outcome = evaluate_gate(make_report(run_id="cur", metrics=current_metrics), baseline)

    assert outcome.failed is False
    assert "efficiency" not in outcome.deltas
    assert any("efficiency" in note for note in outcome.notes)


def test_all_failure_reasons_are_accumulated():
    baseline = make_report(run_id="base", suite_fingerprint="suite-aaaa")
    current = make_report(
        run_id="cur",
        metrics=with_metrics(completion=0.5, safety=0.5, weighted_score=0.5),
        suite_fingerprint="suite-bbbb",
    )

    outcome = evaluate_gate(current, baseline)

    # 一次运行里可能同时有好几处问题，报告要一次说完，否则修完一处再跑一轮才发现下一处。
    assert outcome.failed is True
    assert len([reason for reason in outcome.reasons if reason in outcome.failures]) >= 4
