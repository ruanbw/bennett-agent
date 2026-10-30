"""失败聚簇：调优的输入必须是「共因」，不是一长串个案。

为什么聚簇不和 `loop` 写在一起：

- 「一个失败怎么归到哪个 key」是报告、调优、门禁解释共用的口径，写在两处早晚分叉；
- 聚簇只读 `RunReport` + `SampleVerdict`（不读 `Task` 原文、不读被测系统），
  因此可以完全离线构造数据做测试。

key 的三种形态：

- `item:<rubric item id>`：某个判分项反复拿不到分；
- `gate:<gate id>`：某条硬约束被反复命中（这类最该修，因为它一票否决）；
- `expect:<断言名>`：某个确定性断言反复失败。

断言名只取 `tools: 期望 [...]，实际 [...]` 冒号前的部分：把「期望值/实际值」放进 key，
同一个共因会碎成几十簇，调优就变成对个案打补丁。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from quality.models import Dimension, FailureCluster, RunReport, SampleVerdict

#: 每簇最多保留几条 rationale。三条足够看出共因，再多是重复噪音，还会把候选提示词撑爆。
MAX_RATIONALES = 3

#: 单条 rationale 的截断长度：report 要能一眼读完。
MAX_RATIONALE_CHARS = 240

GATE_PREFIX = "gate:"
ITEM_PREFIX = "item:"
EXPECT_PREFIX = "expect:"


@dataclass
class _Bucket:
    """聚簇过程中的累加器。`task_ids` 用列表 + 去重，保持首次出现的顺序。"""

    dimension: Dimension
    task_ids: list[str] = field(default_factory=list)
    count: int = 0
    rationales: list[str] = field(default_factory=list)


def _clip(text: str) -> str:
    """压成单行并截断：rationale 会进提示词与报告，换行与超长都破坏可读性。"""
    flat = " ".join(str(text).split())
    if len(flat) <= MAX_RATIONALE_CHARS:
        return flat
    return flat[: MAX_RATIONALE_CHARS - 1] + "…"


def assertion_name(failure: str) -> str:
    """从一条 expectation 失败文案里取出稳定的断言名。"""
    head, _, _ = str(failure).partition(":")
    return head.strip() or "unknown"


def cluster_failures(
    report: RunReport,
    verdicts: list[SampleVerdict],
    *,
    min_count: int = 1,
) -> list[FailureCluster]:
    """把失败样本按共因聚簇，count 是「失败样本数」而不是任务数。

    只统计 `passed=False` 的样本：通过的样本里也可能有低分 rubric 项（阈值内的抖动），
    把那些算进来会让「失败聚簇」变成「分数分布统计」，调优会去修没坏的东西。
    """
    categories = {summary.task_id: summary.category for summary in report.tasks}
    failed_items_by_task = {summary.task_id: set(summary.failed_items) for summary in report.tasks}
    buckets: dict[str, _Bucket] = {}

    def record(key: str, dimension: Dimension, task_id: str, rationale: str) -> None:
        bucket = buckets.get(key)
        if bucket is None:
            bucket = buckets[key] = _Bucket(dimension=dimension)
        bucket.count += 1
        if task_id not in bucket.task_ids:
            bucket.task_ids.append(task_id)
        if rationale and len(bucket.rationales) < MAX_RATIONALES:
            clipped = _clip(rationale)
            if clipped not in bucket.rationales:
                bucket.rationales.append(clipped)

    for verdict in verdicts:
        if verdict.passed:
            continue
        task_id = verdict.task_id
        # 没有对应 TaskSummary 时按 completion 记：unknown 维度会让下游 filter 丢数据。
        fallback_dimension: Dimension = categories.get(task_id, "completion")
        score_by_item = {score.item_id: score for score in verdict.scores}

        for gate_id in verdict.gates_failed:
            # gate 一票否决，优先级高于 rubric：所以即使 task 是 completion 类，
            # 也按该任务的 category 归维度，报告里能看出「哪类任务在撞硬约束」。
            record(
                f"{GATE_PREFIX}{gate_id}",
                fallback_dimension,
                task_id,
                f"{task_id}/#{verdict.sample} 命中 gate {gate_id}",
            )

        for item_id in sorted(failed_items_by_task.get(task_id, set())):
            score = score_by_item.get(item_id)
            if score is not None and score.rationale:
                rationale = f"{task_id}/#{verdict.sample} {score.rationale}"
            else:
                rationale = f"{task_id}/#{verdict.sample} 判分项 {item_id} 未达标"
            record(
                f"{ITEM_PREFIX}{item_id}",
                score.dimension if score is not None else fallback_dimension,
                task_id,
                rationale,
            )

        for failure in verdict.expectation_failures:
            record(
                f"{EXPECT_PREFIX}{assertion_name(failure)}",
                fallback_dimension,
                task_id,
                f"{task_id}/#{verdict.sample} {failure}",
            )

    clusters = [
        FailureCluster(
            key=key,
            dimension=bucket.dimension,
            task_ids=bucket.task_ids,
            count=bucket.count,
            sample_rationales=bucket.rationales,
        )
        for key, bucket in buckets.items()
        if bucket.count >= min_count
    ]
    # 次数多的先修；同次数按 key 排序，保证同一份报告每次聚出同样的顺序（可 diff）。
    clusters.sort(key=lambda cluster: (-cluster.count, cluster.key))
    return clusters


__all__ = ["EXPECT_PREFIX", "GATE_PREFIX", "ITEM_PREFIX", "assertion_name", "cluster_failures"]
