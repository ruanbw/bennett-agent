"""命令行层的离线测试：参数表与「判分口径进报告」的接线。

命令行是运维唯一会碰的那一层，所以两类错误代价最大：

1. **接不上的旋钮**：`--fail-over` / `--judge`（scan）/ `--out`（tune）接受了却不生效，
   读命令的人会按一个不存在的模式规划 CI 行为；
2. **进不了报告的判分口径**：判分模型 id 与判分失败次数只活在内存里的话，门禁永远无法
   发现「这份报告跟基线不是同一个判分器 / 有些 0 分来自上游故障」。

这两类都不会让任何函数报错，所以只能用测试钉住。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from quality.cli import _errored_attempts, _graders, _record_judge, build_parser
from quality.models import RunReport


def make_report(config: dict | None = None) -> RunReport:
    return RunReport(
        run_id="run",
        suite_id="core-behavior",
        suite_fingerprint="suite-0001",
        target="fake",
        target_fingerprint="target-0001",
        config=dict(config or {}),
        samples=1,
    )


# =====================================================================
# 参数表：不接受不生效的开关
# =====================================================================


@pytest.mark.parametrize(
    ("argv", "flag"),
    [
        (["scan", "--fail-over", "30"], "--fail-over"),
        (["scan", "--judge", "none"], "--judge"),
        (["tune", "--suite", "x.yaml", "--out", "/tmp/runs"], "--out"),
    ],
)
def test_scan_and_tune_reject_flags_that_never_take_effect(argv: list[str], flag: str) -> None:
    # argparse 对未知参数抛 SystemExit(2)：这是「参数要么生效要么不存在」的机器可验证形式。
    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)


def test_run_still_accepts_judge_and_out() -> None:
    args = build_parser().parse_args(["run", "--suite", "x.yaml", "--judge", "none", "--out", "/tmp/runs"])

    assert args.judge == "none"
    assert args.out == "/tmp/runs"


# =====================================================================
# 判分口径进报告
# =====================================================================


def test_record_judge_writes_model_and_error_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUALITY_JUDGE_MODEL", "group/judge-x")
    args = build_parser().parse_args(["run", "--suite", "x.yaml"])
    graders = _graders(args)
    report = make_report()

    _record_judge(report, graders)

    # 判分模型必须落在报告里，否则门禁无法发现「换了判分器」这件事。
    assert report.config["judge_model"] == "group/judge-x"
    assert report.config["judge_base_url"]
    assert report.config["judge_errors"] == 0


def test_record_judge_counts_failures_after_the_run() -> None:
    args = build_parser().parse_args(["run", "--suite", "x.yaml"])
    graders = _graders(args)
    judge = next(grader for grader in graders if grader.id == "llm")
    report = make_report()

    judge.errors = 2  # 等价于「跑的过程中两次判分调用失败」
    _record_judge(report, graders)

    assert report.config["judge_errors"] == 2


def test_record_judge_without_llm_judge_is_explicit() -> None:
    args = build_parser().parse_args(["run", "--suite", "x.yaml", "--judge", "none"])
    report = make_report()

    _record_judge(report, _graders(args))

    # `--judge none` 也是一个判分口径（没有 LLM 分），要显式写 null 而不是缺键：
    # 缺键会被 `evaluate_gate` 当成「老报告，不比」。
    assert "judge_model" in report.config and report.config["judge_model"] is None
    assert report.config["judge_errors"] == 0


# =====================================================================
# 扫描里「没取得可判证据」的尝试
# =====================================================================


def test_errored_attempts_reads_the_scan_audit_line(tmp_path: Path) -> None:
    from quality.audit import AuditLog

    audit = AuditLog(tmp_path / "audit.jsonl", actor="tester", tenant="default")
    audit.append("scan.start", "run", probes=9)
    audit.append("scan.errored", "run", count=3, samples=["a#0", "b#0", "c#1"])
    audit.append("scan.end", "run", attempts=9)

    assert _errored_attempts(audit) == 3


def test_errored_attempts_is_zero_without_audit_line(tmp_path: Path) -> None:
    from quality.audit import AuditLog

    audit = AuditLog(tmp_path / "audit.jsonl", actor="tester", tenant="default")
    audit.append("scan.end", "run", attempts=9)

    assert _errored_attempts(audit) == 0


def test_cmd_scan_reports_errored_attempts_and_stays_green(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """没跑起来的尝试不能把一个已经挂掉的系统判红，但必须被说出来。"""
    from quality import cli
    from quality.models import SecurityReport

    async def fake_scan(target, suite, *, samples=1, budget=None, audit=None):  # noqa: ANN001 - 替身与 scan 同形
        audit.append("scan.errored", "run-x", count=2, samples=["sec-a#0", "sec-b#0"])
        return SecurityReport(
            run_id="run-x",
            suite_id=suite.id,
            suite_fingerprint=suite.fingerprint(),
            target="fake",
            probes=len(suite.tasks),
            attempts=len(suite.tasks),
            successes=0,
            by_owasp={"A01": 0.0},
            by_severity={"critical": 0, "high": 0, "medium": 0, "low": 0},
            risk_score=0.0,
        )

    monkeypatch.setattr(cli, "scan", fake_scan)
    monkeypatch.setattr(cli, "_target_from_args", lambda args: object())
    args = build_parser().parse_args(["scan", "--out", str(tmp_path), "--run-id", "run-x"])

    assert cli.cmd_scan(args) == 0
    assert "2 次尝试未取得可判证据" in capsys.readouterr().err
