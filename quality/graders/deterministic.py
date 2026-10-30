"""确定性判分：把 `Task.expect` 适配成 `evals.harness` 的断言。

为什么不另写一套断言：`evals/harness.py` 已经在用 `evals/cases.yaml` 判日常回归，
两套断言对「`tools` 空列表 ≠ 不检查工具」这类细节迟早会给出不同结论。这里只做适配
（`Task.expect` → harness `Expectation`，`Trace` → harness `TurnRecord`），判定语义
完全复用 `evals.harness.evaluate`。

一处刻意的限制：`Task.expect` 是一组整体断言，没有「哪条断言对应哪个 rubric 项」的映射。
因此 `judge="deterministic"` 的 rubric 项共享同一个结论——全过 1.0，任一失败 0.0。
想让结构化的部分拿到细粒度分数，要么把断言拆成多条任务，要么用文本 rubric（judge=llm）。

执行器接线（契约里没写、但必须有人做）：
    failures = expectation_failures(task, trace)
    verdict = judge(task, trace, scores=..., gates_failed=..., expectation_failures=failures)
`ExpectationGrader.grade()` 只返回分数，拿不到断言失败；`grade_detailed()` 与模块级
`expectation_failures()` 才是给它输血的入口。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from evals.harness import EvalCase, Expectation, ToolInvocation, TurnRecord, evaluate
from quality.models import Score, Task, Trace

__all__ = [
    "DeterministicOutcome",
    "ExpectationGrader",
    "expectation_failures",
    "to_eval_case",
    "to_turn_record",
]


@dataclass(frozen=True)
class DeterministicOutcome:
    """确定性判分的完整产出。`scores` 进 `judge(scores=...)`，`expectation_failures` 单独传。"""

    scores: list[Score] = field(default_factory=list)
    expectation_failures: list[str] = field(default_factory=list)


def _args_text(payload: dict[str, Any]) -> str:
    """工具参数原文。`Trace.payload["args"]` 是结构化字典，harness 只做字符串比较。"""
    args = payload.get("args")
    if args is None:
        return ""
    return args if isinstance(args, str) else json.dumps(args, ensure_ascii=False, sort_keys=True)


def to_turn_record(trace: Trace) -> TurnRecord:
    """`Trace` → harness 的一轮记录。

    只读 `tool_call` 事件：`tool_result` 是工具说了什么，不是模型做了什么，把它算进
    「调用了哪些工具」会让「失败后是否原样重试」这类断言拿到双倍计数。
    """
    invocations = tuple(
        ToolInvocation(name=event.name or "", args=_args_text(event.payload)) for event in trace.tool_calls
    )
    return TurnRecord(
        tools=invocations,
        answer=trace.final_answer,
        latency_ms=trace.latency_ms,
        context_tokens=trace.input_tokens,
        output_tokens=trace.output_tokens,
        error=trace.error,
    )


def to_eval_case(task: Task) -> EvalCase:
    """`Task` → harness 用例。`expect` 为空（无任何断言）时 harness 会抛 ValueError。"""
    if task.expect is None:
        raise ValueError(f"任务 {task.id} 没有 expect，无法做确定性断言")
    return EvalCase(
        id=task.id,
        prompt=task.prompt,
        tags=list(task.tags),
        requires=list(task.requires),
        expect=Expectation(**task.expect.model_dump(mode="json")),
    )


def expectation_failures(task: Task, trace: Trace) -> list[str]:
    """断言失败原因，形如 `tools: 期望 ['get_current_time']，实际 []`。

    刻意把 harness 的 `check` 名放在最前面：调优阶段要按失败原因聚簇
    （`quality/tuning/diagnose.py`），前缀是稳定可枚举的，`detail` 里全是数值。
    """
    if task.expect is None:
        return []
    try:
        case = to_eval_case(task)
    except ValueError as exc:
        # 用例配置错误（例如 `expect: {}`）不能静默通过，也不能让整轮评测崩掉：
        # 记成一条断言失败，报告里就是「这条任务红了，原因是用例写错了」。
        return [f"expect_invalid: {exc}"]
    return [f"{failure.check}: {failure.detail}" for failure in evaluate(case, to_turn_record(trace))]


class ExpectationGrader:
    """把 `Task.expect` 判成 `expectation_failures` 与 deterministic rubric 项的分数。"""

    id = "deterministic"

    def grade_detailed(self, task: Task, trace: Trace) -> DeterministicOutcome:
        if task.expect is None:
            return DeterministicOutcome()

        failures = expectation_failures(task, trace)
        value = 0.0 if failures else 1.0
        rationale = "; ".join(failures) if failures else "确定性断言全部通过"
        scores = [
            Score(
                item_id=item.id,
                dimension=item.dimension,
                value=value,
                rationale=rationale,
                source="deterministic",
            )
            for item in task.rubric
            if item.judge == "deterministic"
        ]
        return DeterministicOutcome(scores=scores, expectation_failures=failures)

    async def grade(self, task: Task, trace: Trace) -> list[Score]:
        return self.grade_detailed(task, trace).scores
