"""五维指标：把「跑了多少样本、过了多少」翻译成「这份行为能不能发」。

每个口径都是刻意选的，理由写在下面——口径含糊的评测报告比没有报告更危险，
因为它看起来能支持决策。

- `completion` = completion 类任务的平均样本通过率（能力面）。
- `robustness` = **全 k 次都过**的任务比例（pass^k），不是样本平均通过率。
  平均通过率 0.8 有两种完全不同的成因：「每条任务都是八次过六次」（偶发不稳）和
  「八成任务稳过、两成必挂」（缺能力）。单看平均数区分不出来，而只有前者的分数
  波动来自采样噪声。上线决策关心的是「这条能力稳不稳」，所以按「k 次全过」计。
- `safety` = 1 - 命中 gate 的样本比例。安全没有「八成安全」：一次 canary 泄露
  在平均分里只掉 0.1，现实中就是事故。所以按命中率计，且安全项本身走 gate 而不是
  加权分（见 `models.py` 的设计说明）。
- `efficiency` = 1 减去预算占用比例（取最紧的那一项预算）。没有预算信息时返回 1.0
  而不是 0.0：不设预算不等于花得离谱，用 0 会让「没设预算」被读成「效率极差」。
- `pass_at_k` = 至少一次通过的任务比例。它和 pass^k 一起看才能区分「不会做」与
  「做不稳」：pass_at_k 高 + pass^k 低 = 会做但抖，是提示词/工具契约问题；
  两者都低 = 能力缺失，改提示词没用。
- `weighted_score` = 任务平均分（任务等权，rubric 内部权重已折进单条任务的分数）。
  它是平滑量，用来在门禁里发现「还没低于通过线但在变差」的回归，比通过率更早给出信号。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from quality.budget import Budget
from quality.models import DIMENSIONS, SampleVerdict, Task, TaskSummary


def _mean_or_vacuous_pass(values: Sequence[float]) -> float:
    """空集合返回 1.0：一类任务都没有时，不该被算成「这一类全挂」。

    空集算 0 会让「只跑安全套件」的运行在 completion 上显示 0 分，也会让门禁
    误报回归。没有样本就没有证据，没证据不该判负。

    但「一条任务都没跑」不是「这一类没任务」：那种情况由 `compute_metrics` 统一记 0，
    否则预算在开头就耗尽的运行会显示成满分。
    """
    return sum(values) / len(values) if values else 1.0


def _rate(tasks: Sequence[TaskSummary], predicate: Callable[[TaskSummary], bool]) -> float:
    return _mean_or_vacuous_pass([1.0 if predicate(task) else 0.0 for task in tasks])


def summarise(task: Task, verdicts: list[SampleVerdict]) -> TaskSummary:
    """把一条任务的 k 个样本结论压成一行汇总。

    `verdicts` 允许传入整轮的结论（执行器手上就是整轮列表），只统计属于 `task` 的样本。
    """
    mine = [verdict for verdict in verdicts if verdict.task_id == task.id]
    samples = len(mine)
    passed_samples = sum(1 for verdict in mine if verdict.passed)
    score = sum(verdict.score for verdict in mine) / samples if samples else 0.0

    # 维度分按 rubric 权重加权，且**缺项按 0 计**（与 `graders.base.weighted_score` 同一口径）：
    # 判分器没给某一项结论时，那个维度就不算做到，否则「漏判」会变成加分。
    # 权重表从任务里取，因为 `Score` 只带结论不带权重（判分器不必知道自己在整条任务里的比重）。
    buckets: dict[str, list[float]] = {}
    bucket_weights: dict[str, float] = {}
    failed_items: set[str] = set()
    for verdict in mine:
        observed = {item_score.item_id: item_score.value for item_score in verdict.scores}
        for item in task.rubric:
            value = observed.get(item.id, 0.0)
            buckets.setdefault(item.dimension, []).append(value * item.weight)
            bucket_weights[item.dimension] = bucket_weights.get(item.dimension, 0.0) + item.weight
            # 低于本条阈值记一笔：阈值是「这一项算不算做到」，与任务整体通过线不同。
            if value < item.threshold:
                failed_items.add(item.id)

    dimension_scores = {
        dimension: round(sum(values) / bucket_weights[dimension], 6)
        for dimension, values in buckets.items()
        if bucket_weights[dimension] > 0
    }

    gates_failed = sorted({gate for verdict in mine for gate in verdict.gates_failed})
    cost_usd = sum(verdict.trace.cost_usd for verdict in mine)

    return TaskSummary(
        task_id=task.id,
        category=task.category,
        samples=samples,
        passed_samples=passed_samples,
        score=round(score, 6),
        dimension_scores=dimension_scores,
        gates_failed=gates_failed,
        failed_items=sorted(failed_items),
        cost_usd=round(cost_usd, 6),
    )


def _efficiency(budget: Budget | None) -> float:
    """预算占用归一化。取最紧的一项：任一项预算耗尽都会让运行停下，取平均会掩盖它。"""
    if budget is None:
        return 1.0
    usage: list[float] = []
    limits: tuple[tuple[float, float | None], ...] = (
        (budget.usd_used, budget.max_usd),
        (float(budget.tokens_used), None if budget.max_tokens is None else float(budget.max_tokens)),
        (budget.elapsed_seconds, budget.max_seconds),
    )
    for used, limit in limits:
        if limit is None:
            continue
        usage.append(1.0 if limit <= 0 else used / limit)
    if not usage:
        return 1.0
    return round(min(1.0, max(0.0, 1.0 - max(usage))), 6)


def _safety(tasks: Sequence[TaskSummary], verdicts: Sequence[SampleVerdict] | None) -> float:
    """1 - 命中 gate 的样本比例。

    有样本级明细时按样本算（精确）；只有任务汇总时退化为「任务级命中率」——
    后者会把「一条任务偶发命中一次」放大成「这条任务整体不安全」，方向偏保守，
    但不会把命中漏掉。执行器始终传 verdicts，退化路径只给手工构造报告的调用方用。
    """
    safety_tasks = [task for task in tasks if task.category == "safety"]
    if not safety_tasks:
        return 1.0
    if verdicts is not None:
        ids = {task.task_id for task in safety_tasks}
        hits = [1.0 if verdict.gates_failed else 0.0 for verdict in verdicts if verdict.task_id in ids]
        if not hits:
            return 1.0
        return round(1.0 - sum(hits) / len(hits), 6)
    return round(1.0 - _rate(safety_tasks, lambda task: bool(task.gates_failed)), 6)


def compute_metrics(
    tasks: list[TaskSummary],
    samples: int,
    *,
    budget: Budget | None = None,
    verdicts: Sequence[SampleVerdict] | None = None,
) -> dict[str, float]:
    """算齐报告与门禁要用的指标。

    `budget` / `verdicts` 是可选补充信息，不是契约新增的必需参数：没有它们时
    efficiency 返回 1.0、safety 退化为任务级口径，指标仍然可用。契约要求的
    `(tasks, samples)` 两参数调用必须继续成立。
    """
    if samples < 1:
        raise ValueError(f"samples 必须 >= 1，收到 {samples}")

    if not tasks:
        # 一条任务都没跑成（例如预算在第一次派发前就耗尽）：聚合指标一律记 0。
        # 空集合「恒为真」在数学上成立，但在报告里会显示成满分——那是最坏的一种失真。
        return {
            "completion": 0.0,
            "robustness": 0.0,
            "safety": 0.0,
            "efficiency": _efficiency(budget),
            "pass_pow_k": 0.0,
            "pass_at_k": 0.0,
            "weighted_score": 0.0,
        }

    by_category = {dimension: [task for task in tasks if task.category == dimension] for dimension in DIMENSIONS}

    completion = _mean_or_vacuous_pass([task.pass_rate for task in by_category["completion"]])

    # pass^k 要求「跑满 k 次且全过」。样本没跑满（预算中断）时不能记成稳定通过：
    # 少跑的那几次可能正是失败的那几次，声称赞同「侥幸通过」。
    def _stable(task: TaskSummary) -> bool:
        return task.samples == samples and task.samples > 0 and task.passed_samples == task.samples

    robustness = _rate(by_category["robustness"], _stable)
    pass_pow_k = _rate(tasks, _stable)
    pass_at_k = _rate(tasks, lambda task: task.passed_samples > 0)

    # `TaskSummary` 不带 `Task.weight`（冻结契约里没有这个字段），所以这里按任务等权平均。
    # 任务内部的权重差异已经体现在 `TaskSummary.score` 里（`summarise` 按 rubric.weight 算维度分，
    # 判分器按 rubric.weight 算加权分），不会丢。
    weighted = _mean_or_vacuous_pass([task.score for task in tasks])

    return {
        "completion": round(completion, 6),
        "robustness": round(robustness, 6),
        "safety": _safety(tasks, verdicts),
        "efficiency": _efficiency(budget),
        "pass_pow_k": round(pass_pow_k, 6),
        "pass_at_k": round(pass_at_k, 6),
        "weighted_score": round(weighted, 6),
    }
