"""quality 命令行：`python -m quality <run|scan|tune|gate>`。

命令面对的是「今天要不要发版」这个问题，所以退出码是有意义的：
- `run` 只负责产出报告（结论交给 gate）；
- `gate` 与基线比，回归即非 0；
- `scan` 有 critical 命中即非 0——安全没有「分数够高就放行」。

产物固定落 `quality/runs/<suite>/<run_id>/`：报告、逐样本轨迹、审计行。
目录结构是可预期的，CI 与人在同一位置找东西。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from quality.audit import AuditLog
from quality.budget import Budget
from quality.graders.deterministic import ExpectationGrader
from quality.graders.rubric import OpenJudgeClient, RubricJudge
from quality.models import RunReport
from quality.runner.executor import run_suite
from quality.runner.gate import evaluate_gate
from quality.runner.report import write_json
from quality.security.probes import build_security_suite
from quality.security.scan import scan, scan_failed
from quality.suites.loader import Suite, load_suite
from quality.targets.prompt_lab import PromptLabTarget

DEFAULT_RUNS = Path(__file__).resolve().parent / "runs"


def _now_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _target_from_args(args: argparse.Namespace):
    if args.target == "live":
        from quality.targets.live import LiveTarget

        return LiveTarget(base_url=args.base_url or os.environ.get("QUALITY_LIVE_BASE_URL", "http://127.0.0.1:8000"))
    config: dict[str, Any] = {
        "driver": args.driver,
        "model": args.model,
        "base_url": args.base_url,
        "temperature": args.temperature,
        "usd_per_1k_tokens": args.usd_per_1k,
    }
    # scan 子命令没有 --prompt（安全扫描不做提示词调优），用 getattr 取。
    return PromptLabTarget(prompt_text=_read_prompt(getattr(args, "prompt", None)), config=config)


def _read_prompt(path: str | None) -> str | None:
    if not path or path == "-":
        return None
    return Path(path).read_text(encoding="utf-8")


def _graders(args: argparse.Namespace) -> list[Any]:
    graders: list[Any] = [ExpectationGrader()]
    if args.judge == "llm":
        graders.append(RubricJudge(OpenJudgeClient()))
    return graders


def _suite_for_judge(suite: Suite, args: argparse.Namespace) -> Suite:
    """`--judge none` 时把「只能靠 LLM 判」的任务整条摘掉。

    不这么做会有两个坏结果：LLM rubric 项被记 0 分（把「没判」伪装成「没做到」），
    或者干脆被加权平均兜底成通过（把判分器的空缺变成被测系统的加分）。两者都会让
    离线跑出来的数字与真跑出来的数字不可比，所以宁可少跑几条并把缺的口感说清楚。
    """
    if args.judge != "none":
        return suite
    kept = [
        task
        for task in suite.tasks
        if task.expect is not None or task.gates or any(item.judge != "llm" for item in task.rubric)
    ]
    dropped = [task.id for task in suite.tasks if task not in kept]
    if dropped:
        print(
            f"提示    --judge none：{len(dropped)} 条只能靠 LLM 判分的任务未参与 "
            f"（{', '.join(dropped[:6])}{' …' if len(dropped) > 6 else ''}）",
            file=sys.stderr,
        )
    return suite.model_copy(update={"tasks": kept})


def _budget(args: argparse.Namespace) -> Budget | None:
    if args.budget_usd is None and args.budget_tokens is None and args.budget_seconds is None:
        return None
    return Budget(max_usd=args.budget_usd, max_tokens=args.budget_tokens, max_seconds=args.budget_seconds)


def _audit(args: argparse.Namespace, path: Path) -> AuditLog:
    return AuditLog(path, actor=args.actor, tenant=args.tenant)


def _print_run(report: RunReport) -> None:
    print(f"run     {report.run_id}  suite={report.suite_id}  target={report.target}")
    print(f"指纹    suite={report.suite_fingerprint}  target={report.target_fingerprint}")
    print(f"成本    ${report.cost_usd:.4f}  耗时 {report.duration_ms / 1000:.1f}s  样本×{report.samples}")
    print("指标    " + "  ".join(f"{name}={value:.3f}" for name, value in sorted(report.metrics.items())))
    failed = [task for task in report.tasks if task.passed_samples < task.samples]
    print(f"任务    {len(report.tasks) - len(failed)}/{len(report.tasks)} 全部通过")
    for task in failed[:12]:
        gates = f" gates={','.join(task.gates_failed)}" if task.gates_failed else ""
        print(f"  - {task.task_id:<28} {task.passed_samples}/{task.samples} 分={task.score:.2f}{gates}")
    if len(failed) > 12:
        print(f"  ... 另有 {len(failed) - 12} 条未全部通过")


def cmd_run(args: argparse.Namespace) -> int:
    suite = _suite_for_judge(load_suite(args.suite), args)
    run_id = args.run_id or _now_id()
    out = Path(args.out) / suite.id / run_id if args.out else DEFAULT_RUNS / suite.id / run_id
    audit = _audit(args, out / "audit.jsonl")
    target = _target_from_args(args)
    report, verdicts = asyncio.run(
        run_suite(
            target,
            suite,
            samples=args.samples,
            concurrency=args.concurrency,
            graders=_graders(args),
            budget=_budget(args),
            audit=audit,
            run_id=run_id,
            config={"judge": args.judge, "driver": args.driver, "target": args.target},
        )
    )
    report.audit_ref = str(out / "audit.jsonl")
    write_json(report, verdicts, out)
    try:
        from quality.runner.report import render_html

        html_path = render_html(report, verdicts, out / "report.html")
        print(f"报告    {html_path}")
    except Exception as exc:  # 报告渲染失败不该抹掉已经跑出来的结论
        print(f"警告    HTML 报告渲染失败：{type(exc).__name__}: {exc}", file=sys.stderr)
    print(f"JSON    {out / 'report.json'}")
    _print_run(report)
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    suite = build_security_suite()
    run_id = args.run_id or _now_id()
    out = Path(args.out) / "security" / run_id if args.out else DEFAULT_RUNS / "security" / run_id
    target = _target_from_args(args)
    report = asyncio.run(
        scan(
            target,
            suite,
            samples=args.samples,
            budget=_budget(args),
            audit=_audit(args, out / "audit.jsonl"),
        )
    )
    out.mkdir(parents=True, exist_ok=True)
    payload = report.model_dump(mode="json")
    (out / "security.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"探针    {report.probes} 条 × {args.samples} 次 = {report.attempts} 次尝试")
    print(f"ASR     {report.attack_success_rate:.1%}   风险分 {report.risk_score:.1f}/100")
    print("OWASP   " + "  ".join(f"{code}={rate:.1%}" for code, rate in sorted(report.by_owasp.items())))
    for finding in report.findings[:10]:
        print(f"  ! [{finding.severity}] {finding.owasp} {finding.probe_id}: {finding.evidence[:100]}")
    print(f"JSON    {out / 'security.json'}")
    return 1 if report.risk_score >= args.fail_over or scan_failed(report) else 0


def cmd_gate(args: argparse.Namespace) -> int:
    current = _load_report(args.current)
    baseline = _load_report(args.baseline)
    outcome = evaluate_gate(current, baseline, min_score_delta=args.min_delta)
    print(f"当前 {current.run_id}  vs  基线 {baseline.run_id}")
    for name, delta in sorted(outcome.deltas.items()):
        print(f"  {name:<16} {delta:+.4f}")
    if outcome.failed:
        print("结论：不放行")
        for reason in outcome.reasons:
            print(f"  - {reason}")
        return 1
    print("结论：放行")
    return 0


def _load_report(directory: str | Path) -> RunReport:
    path = Path(directory)
    if path.is_dir():
        path = path / "report.json"
    if not path.exists():
        raise SystemExit(f"找不到报告：{path}")
    return RunReport.model_validate(json.loads(path.read_text(encoding="utf-8")))


def cmd_tune(args: argparse.Namespace) -> int:
    from quality.tuning.loop import optimize
    from quality.tuning.propose import HeuristicProposer, LLMProposer

    suite = _suite_for_judge(load_suite(args.suite), args)
    base_prompt = _read_prompt(args.prompt)
    base_target = PromptLabTarget(prompt_text=base_prompt, config={"driver": args.driver, "model": args.model})
    prompt_text = base_target.system_prompt()
    run_id = args.run_id or _now_id()
    audit = _audit(args, DEFAULT_RUNS / "tuning" / run_id / "audit.jsonl")

    base_report, base_verdicts = asyncio.run(
        run_suite(
            base_target,
            suite,
            samples=args.samples,
            concurrency=args.concurrency,
            graders=_graders(args),
            budget=_budget(args),
            run_id=f"{run_id}-base",
            audit=audit,
        )
    )
    print(
        f"基线    weighted={base_report.metrics.get('weighted_score', 0):.3f}  "
        f"pass@k={base_report.metrics.get('pass_at_k', 0):.3f}"
    )

    proposer = HeuristicProposer()
    if args.judge == "llm":
        proposer = LLMProposer(OpenJudgeClient())

    def factory(text: str) -> PromptLabTarget:
        return PromptLabTarget(prompt_text=text, config={"driver": args.driver, "model": args.model})

    report = asyncio.run(
        optimize(
            factory,
            suite,
            base_report,
            base_verdicts,
            proposer=proposer,
            prompt_text=prompt_text,
            iterations=args.iterations,
            samples=args.samples,
            graders=_graders(args),
            budget=_budget(args),
            audit=audit,
        )
    )
    out = DEFAULT_RUNS / "tuning" / report.run_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "tuning.json").write_text(
        json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"候选    {len(report.candidates)} 个，Pareto 前沿 {len(report.pareto_front)} 个")
    for candidate in report.candidates:
        print(
            f"  - {candidate.id:<24} weighted={candidate.metrics.get('weighted_score', 0):.3f} "
            f"cost=${candidate.cost_usd:.4f} pass={candidate.passed}"
        )
    print(f"入选    {report.selected or '无（无候选在不降低 safety 的前提下超过基线）'}")
    if report.stopped_reason:
        print(f"停止    {report.stopped_reason}")
    print(f"JSON    {out / 'tuning.json'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quality", description="bennett-agent 评测 / 调优 / 安全管控")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--target", choices=["prompt-lab", "live"], default="prompt-lab")
        p.add_argument(
            "--driver",
            choices=["openai", "scripted"],
            default="openai",
            help="openai=打真实模型（默认）；scripted=按用例脚本回放，仅用于框架自测",
        )
        p.add_argument(
            "--judge",
            choices=["llm", "none"],
            default="llm",
            help="llm=rubric 交给 LLM 判；none=只跑确定性断言与安全 gate",
        )
        p.add_argument("--model", default=os.environ.get("QUALITY_MODEL", "group/deepseek-v4.1-flash"))
        p.add_argument("--base-url", default=None)
        p.add_argument("--temperature", type=float, default=0.0)
        p.add_argument("--usd-per-1k", type=float, default=0.0, help="每 1000 token 的美元成本，用于效率指标")
        p.add_argument("--samples", type=int, default=1, help="每条任务采样次数 k；pass^k 用它衡量稳定性")
        p.add_argument("--concurrency", type=int, default=4)
        p.add_argument("--budget-usd", type=float, default=None)
        p.add_argument("--budget-tokens", type=int, default=None)
        p.add_argument("--budget-seconds", type=float, default=None)
        p.add_argument("--out", default=None, help="产物根目录（默认 quality/runs）")
        p.add_argument("--run-id", default=None)
        p.add_argument("--actor", default=os.environ.get("QUALITY_ACTOR", "local"))
        p.add_argument("--tenant", default=os.environ.get("QUALITY_TENANT", "default"))

    run = sub.add_parser("run", help="跑评测套件")
    run.add_argument("--suite", required=True)
    run.add_argument("--prompt", default=None, help="候选提示词文件；不给则渲染生产模板")
    common(run)
    run.set_defaults(func=cmd_run)

    scan_p = sub.add_parser("scan", help="跑安全探针")
    scan_p.add_argument("--fail-over", type=float, default=100.0, help="风险分达到该值即非 0 退出")
    common(scan_p)
    scan_p.set_defaults(func=cmd_scan)

    gate = sub.add_parser("gate", help="与基线报告比较，决定是否放行")
    gate.add_argument("--current", required=True)
    gate.add_argument("--baseline", required=True)
    gate.add_argument("--min-delta", type=float, default=-0.02)
    gate.set_defaults(func=cmd_gate, actor="local", tenant="default")

    tune = sub.add_parser("tune", help="reflection 调优闭环")
    tune.add_argument("--suite", required=True)
    tune.add_argument("--prompt", default=None)
    tune.add_argument("--iterations", type=int, default=2)
    common(tune)
    tune.set_defaults(func=cmd_tune)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
