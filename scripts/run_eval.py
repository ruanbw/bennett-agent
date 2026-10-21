"""行为回归评测 CLI。

    uv run python scripts/run_eval.py                 # 全量，与同配置的上一次对比
    uv run python scripts/run_eval.py --list          # 只列用例，不连网关
    uv run python scripts/run_eval.py --tag clarify   # 按标签过滤
    uv run python scripts/run_eval.py --model openai/gpt-4o-mini --effort low
    uv run python scripts/run_eval.py --baseline none # 第一次跑，不比较

退出码：0 全部通过；1 有失败；2 配置/用例文件有问题（不是模型的问题）。

**这个脚本会真的调用上游模型并产生费用**，所以它不在 CI 里跑，只作为人工回归工具。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# 允许 `python scripts/run_eval.py` 直接跑（否则拿不到仓库根的 evals 包）
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from evals.harness import (  # noqa: E402
    CaseResult,
    EvalCase,
    Totals,
    TurnRecord,
    baseline_config_mismatch,
    build_report,
    diff_reports,
    load_cases,
    make_result,
    missing_requirements,
    read_report,
    select_baseline,
    totals_of,
    write_report,
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

DEFAULT_CASES = REPO_ROOT / "evals" / "cases.yaml"
DEFAULT_RUNS_DIR = REPO_ROOT / "evals" / "runs"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_eval.py",
        description="跑 Bennett Agent 的行为回归评测（会真实调用上游模型）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "网关地址与密钥默认取自 .env；--base-url/--api-key 只覆盖本次进程，"
            "也可以直接用环境变量 LITELLM_API_BASE / LITELLM_API_KEY（不会进 shell history）。"
        ),
    )
    parser.add_argument("--cases", default=str(DEFAULT_CASES), help="用例文件路径（默认 evals/cases.yaml）")
    parser.add_argument("--model", default=None, help="模型路由键；缺省用 settings.MODEL_NAME")
    parser.add_argument("--effort", default=None, help="思考等级（透传给上游，例如 low/high/off）")
    parser.add_argument("--base-url", default=None, help="覆盖 LITELLM_API_BASE")
    parser.add_argument("--api-key", default=None, help="覆盖 LITELLM_API_KEY（优先用环境变量）")
    parser.add_argument("--only", default=None, help="只跑这些用例，逗号分隔的 id")
    parser.add_argument("--tag", default=None, help="只跑带该 tag 的用例")
    parser.add_argument(
        "--baseline",
        default="auto",
        help=(
            "auto = 同模型 + 同思考等级 + 同上下文守卫的最近一份报告；"
            "none = 不比较；其他值按路径读取（配置不一致会先警告）"
        ),
    )
    parser.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR), help="报告目录（默认 evals/runs）")
    parser.add_argument("--timeout", type=float, default=180.0, help="单条用例超时秒数（默认 180）")
    parser.add_argument(
        "--context-guard",
        action="store_true",
        help="保留项目的上下文预算判定（默认关掉：短用例用不上，且会多查一次模型窗口）",
    )
    parser.add_argument("--list", action="store_true", help="只列出用例，不跑")
    return parser


def slugify(value: str) -> str:
    """把模型 id 变成安全的目录名。"""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "model"


def select_cases(cases: list[EvalCase], only: str | None, tag: str | None) -> list[EvalCase]:
    selected = cases
    if only:
        wanted = [item.strip() for item in only.split(",") if item.strip()]
        known = {case.id for case in cases}
        unknown = [item for item in wanted if item not in known]
        if unknown:
            raise ValueError(f"--only 里有不存在的用例：{unknown}")
        selected = [case for case in selected if case.id in set(wanted)]
    if tag:
        selected = [case for case in selected if tag in case.tags]
        if not selected:
            raise ValueError(f"没有带 tag={tag!r} 的用例")
    if not selected:
        raise ValueError("没有选中任何用例")
    return selected


def list_cases(cases: list[EvalCase]) -> None:
    width = max(len(case.id) for case in cases)
    for case in cases:
        tags = ",".join(case.tags) or "-"
        requires = f" requires={','.join(case.requires)}" if case.requires else ""
        print(f"{case.id:<{width}}  [{tags}]{requires}  {case.prompt}")


def print_diff(baseline_path: Path, baseline: dict, diff) -> None:
    passed = baseline.get("totals", {}).get("passed", 0)
    judged = passed + baseline.get("totals", {}).get("failed", 0)
    print()
    print(f"基线对比（{baseline_path.name}，通过 {passed}/{judged}）")
    if diff.newly_failed:
        print(f"  ✗ 新失败：{', '.join(diff.newly_failed)}")
    if diff.newly_passed:
        print(f"  ✓ 新通过：{', '.join(diff.newly_passed)}")
    if diff.still_failed:
        print(f"  · 仍然失败：{', '.join(diff.still_failed)}")
    if diff.changed_failures:
        print(f"  ! 仍然失败但原因变了：{', '.join(diff.changed_failures)}")
    if diff.newly_skipped:
        print(f"  · 本次跳过：{', '.join(diff.newly_skipped)}")
    if diff.added:
        print(f"  + 新增用例：{', '.join(diff.added)}")
    if diff.missing:
        print(f"  - 基线里有、本次没有：{', '.join(diff.missing)}")
    # 「行为一致」只在**状态完全没变**时说。此前判断漏了新增/缺失用例与仍然失败的用例：
    # 全部仍失败时会打印「行为与基线一致」，读起来像「没有问题了」。
    if diff.is_unchanged:
        print("  行为与基线一致")
    print(
        f"  token：上下文 {diff.context_tokens_delta:+d}，输出 {diff.output_tokens_delta:+d}"
        f"；耗时 {diff.latency_ms_delta / 1000:+.1f}s",
    )


def print_summary(totals: Totals, report_path: Path) -> None:
    print()
    parts = [f"通过 {totals.passed}/{totals.judged}"]
    if totals.skipped:
        parts.append(f"跳过 {totals.skipped}")
    print(" · ".join(parts))
    print(
        f"上下文 {totals.context_tokens:,} tok · 输出 {totals.output_tokens:,} tok"
        f" · 总耗时 {totals.latency_ms / 1000:.1f}s",
    )
    print(f"报告：{report_path}")


def print_case_line(index: int, total: int, result: CaseResult) -> None:
    label = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}[result.status]
    record = result.record
    detail = ""
    if result.status == "skip":
        detail = f"  {result.skipped_reason}"
    else:
        detail = f"  {record.latency_ms / 1000:5.1f}s"
        if record.context_tokens is not None:
            detail += f"  ctx {record.context_tokens:,} / out {record.output_tokens or 0:,}"
        if record.fallback_to:
            detail += f"  fallback→{record.fallback_to}"
    print(f"[{index}/{total}] {result.case_id:<28} {label}{detail}", flush=True)
    for failure in result.failures:
        print(f"         - {failure.check}: {failure.detail}", flush=True)


async def run_all(cases: list[EvalCase], args: argparse.Namespace, model_key: str) -> list[CaseResult]:
    # 延迟导入：runner 会 import app.*，而 app.config 在导入时就固化环境变量。
    # 因此 --base-url/--api-key/上下文守卫的覆盖必须先写进 os.environ（见 main）。
    from evals.runner import available_requirements, run_case

    available = available_requirements()
    results: list[CaseResult] = []
    for index, case in enumerate(cases, start=1):
        if missing_requirements(case, available):
            result = make_result(case, TurnRecord(), available=available)
        else:
            record = await run_case(
                case,
                model_key=model_key,
                reasoning_effort=args.effort,
                timeout_seconds=args.timeout,
            )
            result = make_result(case, record, available)
        print_case_line(index, len(cases), result)
        results.append(result)
    return results


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        cases = load_cases(args.cases)
        selected = select_cases(cases, args.only, args.tag)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_USAGE

    if args.list:
        list_cases(selected)
        return EXIT_OK

    # 环境变量必须在 import app.config 之前写入：Settings 在模块导入时实例化。
    # 系统环境变量优先于 .env.*，因此这里的覆盖会生效且不影响磁盘上的配置。
    if args.base_url:
        os.environ["LITELLM_API_BASE"] = args.base_url
    if args.api_key:
        os.environ["LITELLM_API_KEY"] = args.api_key
    if not args.context_guard:
        os.environ["CONTEXT_GUARD_MODE"] = "off"

    from app.config import settings
    from evals.runner import safe_endpoint

    model_key = (args.model or settings.model_name or "").strip()
    if not model_key:
        print("错误：没有可用的模型，请用 --model 指定或设置 MODEL_NAME", file=sys.stderr)
        return EXIT_USAGE

    run_dir = Path(args.runs_dir) / slugify(model_key)
    baseline_path = select_baseline(
        args.baseline,
        run_dir,
        model=model_key,
        reasoning_effort=args.effort,
        context_guard=settings.context_guard_mode,
    )
    if baseline_path is None and args.baseline.strip().lower() == "auto":
        print(
            "提示：没有找到同模型 / 同思考等级 / 同上下文守卫的历史报告，本次不做基线对比。",
            file=sys.stderr,
        )

    started_at = datetime.now(timezone.utc)
    effort_label = args.effort or "默认"
    print(
        f"模型：{model_key}  · 思考等级：{effort_label}  · 上下文守卫：{settings.context_guard_mode}"
        f"  · 用例：{len(selected)}  · 网关：{settings.litellm_api_base}",
    )
    results = asyncio.run(run_all(selected, args, model_key))

    report = build_report(
        cases=selected,
        results=results,
        model=model_key,
        reasoning_effort=args.effort,
        started_at=started_at.isoformat(timespec="seconds"),
        extra={
            "endpoint": safe_endpoint(settings.litellm_api_base),
            "app_env": settings.app_env,
            "assistant_name": settings.assistant_name,
            "context_guard": settings.context_guard_mode,
        },
    )
    report_path = write_report(report, run_dir / f"{started_at:%Y%m%dT%H%M%SZ}.json")

    totals = totals_of(results)
    print_summary(totals, report_path)

    if baseline_path is not None:
        try:
            baseline = read_report(baseline_path)
        except ValueError as exc:
            print(f"提示：基线无法读取，跳过对比（{exc}）", file=sys.stderr)
        else:
            # auto 已经按行为配置筛过；只有用户显式指定路径时才可能不一致。
            mismatch = baseline_config_mismatch(
                baseline,
                model=model_key,
                reasoning_effort=args.effort,
                context_guard=settings.context_guard_mode,
            )
            if mismatch:
                print("警告：基线来自不同配置，下面的差异未必是行为回归：", file=sys.stderr)
                for item in mismatch:
                    print(f"  - {item}", file=sys.stderr)
            print_diff(baseline_path, baseline, diff_reports(baseline, report))

    return EXIT_FAILED if totals.failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
