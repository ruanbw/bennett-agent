"""执行器：把 (target, suite, samples) 跑成一份报告。

评测执行器的三条硬要求，写法都是围绕它们来的：

1. **整轮不能崩。** 一条任务炸掉、一个判分器崩掉、安全探测器抛异常，都只能降级成
   这条样本的「不通过 + 原因」，不能把整轮结果丢掉。理由很实际：全量回归跑一次要
   几分钟到几十分钟，中途一个评分器的小 bug 让 200 条结果白跑，没人会再跑第二遍。
   降级时一律判**不通过**（而不是跳过）：没拿到可判的行为就不算通过，
   「判不了=通过」是评测平台最常见的失真方式。
2. **并发不改变结论。** 并发只影响完成顺序，不影响任何一条样本的判分；落盘时按
   (任务序, 样本号) 重排，保证同样输入两次跑出同样的文件（报告要能进 git diff）。
3. **预算耗尽要停下并说清楚。** 已派发的样本不打断（缺样本比超支更难解释），
   但不再派发新的，并在 `report.config` 里写 `partial: true` + `stopped_reason`。
   门禁看到 partial 就拒绝放行——半轮数据不足以支持「没有回归」的结论。

并发控制用 `asyncio.Semaphore` 而不是「一次 gather 全部」：gather 全部任务会让
排队中的协程也持有输入参数并全部进入事件循环，预算一停只能靠每个协程自己检查，
而 Semaphore 版本在**拿到执行权之后**才检查预算，语义更接近「工位空出来了才看还有没有料」。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from quality.audit import AuditLog
from quality.budget import Budget
from quality.graders.base import judge
from quality.graders.deterministic import expectation_failures
from quality.graders.security import evaluate_gates
from quality.models import RunReport, SampleVerdict, Score, Task, TaskSummary, Trace
from quality.runner.metrics import compute_metrics, summarise
from quality.targets.base import Target

if TYPE_CHECKING:  # pragma: no cover - 只为类型检查，运行时不 import 契约外模块
    from quality.graders.base import Grader
    from quality.suites.loader import Suite

#: 评测层自身出问题时写进 `expectation_failures` 的前缀。
#: 用同一个前缀而不是各写一套，是为了让报告里能一眼把「被测错了」和「评测自己坏了」分开。
EVAL_ERROR_PREFIX = "eval_error:"


def _now_run_id() -> str:
    """默认 run_id：UTC 秒级时间戳。排序即时间序，跨时区不会错位。"""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _safe_fingerprint(callable_: Any, fallback: str) -> str:
    """指纹算不出来时给一个明确的占位值，而不是让整轮跑挂掉。

    报告里出现 `unknown` 是可见的问题；抛异常则是把「复现性缺失」变成「今天没有报告」。
    """
    try:
        return str(callable_())
    except Exception:  # noqa: BLE001 - 见上面：指纹失败不该中断运行
        return fallback


async def _safe_run(target: Target, task: Task, sample: int) -> Trace:
    """执行一次任务。协议要求 target 自己不抛异常，但协议不是运行时保证。"""
    try:
        trace = await target.run(task, sample)
    except asyncio.CancelledError:
        # 取消要透传：吞掉会把「上层不想跑了」变成「这条样本失败」，导致停机时报告失真。
        raise
    except Exception as exc:  # noqa: BLE001 - 单条样本的失败必须降级，见模块说明第 1 条
        return Trace(
            task_id=task.id,
            sample=sample,
            error=f"{type(exc).__name__}: {exc}",
            config_fingerprint=_safe_fingerprint(target.config_fingerprint, "unknown"),
        )
    if not isinstance(trace, Trace):
        # 鸭子类型的 target 可能返回 dict 或 None；换成明确的 Trace.error，
        # 比让 `trace.events` 在后面某个判分器里炸出 AttributeError 更好定位。
        return Trace(
            task_id=task.id,
            sample=sample,
            error=f"target 返回了 {type(trace).__name__}，不是 Trace",
            config_fingerprint=_safe_fingerprint(target.config_fingerprint, "unknown"),
        )
    return trace


async def _judge_trace(task: Task, trace: Trace, graders: Sequence[Grader]) -> SampleVerdict:
    """跑全部判分器与安全 gate，交给唯一的判分入口 `judge`。

    两处接线契约里没写、但必须有人做（`graders/deterministic.py` 的模块注释也这么写）：

    - `Grader.grade()` 只回分数，拿不到断言失败，所以 `Task.expect` 单独判一次；
      `expect` 是任务自己声明的断言，不该取决于调用方今天挂了哪些判分器。
    - `Trace.error` **不在这里**记成失败。执行失败归哪个维度取决于任务：有 expect 的
      任务会走上面那条得到断言失败，有 rubric 的任务由 rubric 判分器记 0 分；而安全探针
      「没跑起来」不等于「被攻破」，在这里一刀切会给 ASR 统计造假象（见 `graders/base.py`）。
    """
    problems: list[str] = []
    scores: list[Score] = []
    for grader in graders:
        try:
            scores.extend(await grader.grade(task, trace))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 判分器崩溃等价于「没拿到分」
            problems.append(f"{EVAL_ERROR_PREFIX}grader:{getattr(grader, 'id', '?')}: {type(exc).__name__}: {exc}")

    try:
        # 断言失败原因形如 `tools: 期望 ['get_current_time']，实际 []`，前缀是调优聚簇的 key。
        problems.extend(expectation_failures(task, trace))
    except Exception as exc:  # noqa: BLE001 - 断言本身崩了也不能当成「断言通过」
        problems.append(f"{EVAL_ERROR_PREFIX}expect: {type(exc).__name__}: {exc}")

    gates_failed: list[str] = []
    if task.gates:
        try:
            gates_failed = list(evaluate_gates(task, trace))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 探测器抛异常绝不能当成「没命中」
            problems.append(f"{EVAL_ERROR_PREFIX}gate: {type(exc).__name__}: {exc}")

    # passed 只由 judge 算，executor 不重算——否则「判分口径」会有两份实现并慢慢分叉。
    return judge(task, trace, scores=scores, gates_failed=gates_failed, expectation_failures=problems)


async def _run_one(
    target: Target,
    task: Task,
    sample: int,
    *,
    graders: Sequence[Grader],
    budget: Budget | None,
) -> SampleVerdict:
    trace = await _safe_run(target, task, sample)
    if budget is not None:
        # 每个样本都记账：预算是运行期资源，等一轮跑完再算就晚了。
        budget.charge(tokens=trace.input_tokens + trace.output_tokens, usd=trace.cost_usd)
    return await _judge_trace(task, trace, graders)


def _gate_severities(suite: Suite) -> dict[str, str]:
    """把 `{gate_id: severity}` 写进报告，供 `runner.gate` 判 critical。

    `TaskSummary` 只留 gate id，severity 在冻结契约里没有位置；不把这个信息带出去，
    门禁就只能靠「gate id 里有没有 critical 字样」猜。写进 config 是契约内的扩展点。
    """
    severities: dict[str, str] = {}
    for task in suite.tasks:
        for gate in task.gates:
            severities.setdefault(gate.id, gate.severity)
    return severities


async def run_suite(
    target: Target,
    suite: Suite,
    *,
    samples: int,
    concurrency: int = 4,
    graders: list[Grader] | None = None,
    budget: Budget | None = None,
    config: dict[str, Any] | None = None,
    audit: AuditLog | None = None,
    run_id: str | None = None,
) -> tuple[RunReport, list[SampleVerdict]]:
    """跑 `suite.tasks × samples` 次，返回报告与逐样本结论。"""
    if samples < 1:
        raise ValueError(f"samples 必须 >= 1，收到 {samples}")
    if concurrency < 1:
        raise ValueError(f"concurrency 必须 >= 1，收到 {concurrency}")

    started = time.monotonic()
    run_id = run_id or _now_run_id()
    grader_list = list(graders or [])
    units = [(task, sample) for task in suite.tasks for sample in range(samples)]
    semaphore = asyncio.Semaphore(concurrency)

    # index → verdict：并发完成顺序不可预测，用下标在最后还原稳定顺序。
    results: dict[int, SampleVerdict] = {}
    skipped: list[tuple[str, int]] = []

    suite_fingerprint = _safe_fingerprint(suite.fingerprint, "unknown")
    target_fingerprint = _safe_fingerprint(target.config_fingerprint, "unknown")
    target_name = str(getattr(target, "name", type(target).__name__))

    if audit is not None:
        audit.append(
            "run.start",
            f"{suite.id}@{run_id}",
            suite=suite.id,
            run_id=run_id,
            target=target_name,
            samples=samples,
            concurrency=concurrency,
            suite_fingerprint=suite_fingerprint,
            target_fingerprint=target_fingerprint,
        )

    async def execute(index: int, task: Task, sample: int) -> None:
        async with semaphore:
            # 预算检查放在拿到执行权之后：排队中的样本不该在预算耗尽后还偷偷开跑。
            if budget is not None and budget.check():
                skipped.append((task.id, sample))
                return
            results[index] = await _run_one(target, task, sample, graders=grader_list, budget=budget)

    await asyncio.gather(*(execute(index, task, sample) for index, (task, sample) in enumerate(units)))

    verdicts = [results[index] for index in sorted(results)]
    summaries: list[TaskSummary] = [summarise(task, verdicts) for task in suite.tasks]
    # 一条样本都没跑成的任务（预算在它之前就耗尽）不进任务表：把「没跑」记成 0 分会伪造回归。
    # 未跑的部分由 config.partial / config.skipped_samples 交代。
    summaries = [summary for summary in summaries if summary.samples > 0]

    partial = bool(skipped)
    stopped_reason = budget.stopped_reason if budget is not None else None
    report_config: dict[str, Any] = dict(config or {})
    report_config.update(
        {
            "samples": samples,
            "concurrency": concurrency,
            "graders": [str(getattr(grader, "id", type(grader).__name__)) for grader in grader_list],
            "gate_severities": _gate_severities(suite),
            "partial": partial,
            "skipped_samples": len(skipped),
            "budget": budget.to_dict() if budget is not None else None,
        }
    )
    if partial and stopped_reason:
        report_config["stopped_reason"] = stopped_reason

    duration_ms = int((time.monotonic() - started) * 1000)
    cost_usd = sum(verdict.trace.cost_usd for verdict in verdicts)
    tokens = sum(verdict.trace.input_tokens + verdict.trace.output_tokens for verdict in verdicts)
    metrics = compute_metrics(summaries, samples, budget=budget, verdicts=verdicts)

    report = RunReport(
        run_id=run_id,
        suite_id=suite.id,
        suite_fingerprint=suite_fingerprint,
        target=target_name,
        target_fingerprint=target_fingerprint,
        config=report_config,
        samples=samples,
        tasks=summaries,
        metrics=metrics,
        totals={
            "tasks": float(len(summaries)),
            "verdicts": float(len(verdicts)),
            "passed": float(sum(1 for verdict in verdicts if verdict.passed)),
            "samples": float(samples),
            "tokens": float(tokens),
            "cost_usd": round(cost_usd, 6),
        },
        duration_ms=duration_ms,
        cost_usd=round(cost_usd, 6),
        audit_ref=str(audit.path) if audit is not None else None,
    )

    if audit is not None:
        audit.append(
            "run.finish",
            f"{suite.id}@{run_id}",
            outcome="partial" if partial else "ok",
            run_id=run_id,
            tasks=len(summaries),
            verdicts=len(verdicts),
            passed=report.totals["passed"],
            cost_usd=report.cost_usd,
            duration_ms=duration_ms,
            partial=partial,
            stopped_reason=stopped_reason or "",
            metrics=metrics,
        )

    return report, verdicts


__all__ = ["EVAL_ERROR_PREFIX", "run_suite"]
