"""quality 层的冻结契约：所有生产者与消费者只认这里的类型。

冻结的意思：改字段 = 改契约，必须同时改 `docs/design/quality-platform.md` 与
`tests/test_quality_contracts.py`，否则下游按旧字段读报告会静默丢数据。

设计口径（与开源实现的对应关系）：

- `Task` / `RubricItem`：Claw-Eval 的「人类校验任务 + rubric」，OpenJudge 的 grader 粒度。
- `Gate`：OpenJudge 的「门禁先于加权分」（GatedWeightedSum）：硬约束不过，
  总分多高都判不通过。安全项一律走 gate，不走加权——加权等于承认「可以拿分数换泄露」。
- `Trace`：轨迹透明。判分只读 Trace，不读被测系统内部状态，否则评测与被测耦合。
- `SampleVerdict.passed` 与 `pass^k`：单次侥幸不算通过。k 次全过才算这条任务稳定通过。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: 评测维度。顺序即报告顺序。
DIMENSIONS: tuple[str, ...] = ("completion", "robustness", "safety", "efficiency")

Dimension = Literal["completion", "robustness", "safety", "efficiency"]
Severity = Literal["low", "medium", "high", "critical"]
JudgeKind = Literal["llm", "deterministic", "detector"]

#: 单条 rubric 的满分刻度（0 = 完全没做到，3 = 完全做到）。归一化后是 value / 3。
SCORE_SCALE = 3.0


def _canonical(payload: Any) -> str:
    """稳定序列化：同样的内容必须得到同样的哈希，键序与空白都不能影响指纹。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(payload: Any) -> str:
    """内容指纹，用于「这份报告跑的是哪版用例/哪版配置」。"""
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()[:16]


class RubricItem(BaseModel):
    """一条判分项。description 必须是「可被判定的行为」，不能是「回答得好不好」。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    dimension: Dimension
    description: str = Field(min_length=1)
    #: 相对权重；同一条任务内归一化，因此只比较相对大小。
    weight: float = Field(default=1.0, gt=0)
    #: 该项归一化分数低于此值即视为「这一项没做到」，会写进报告但不必然判失败。
    threshold: float = Field(default=0.6, ge=0, le=1)
    judge: JudgeKind = "llm"


class Gate(BaseModel):
    """硬约束。detector 名字解析见 `quality.graders.security.DETECTORS`。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    detector: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    severity: Severity = "high"


class Expectation(BaseModel):
    """确定性断言（可选）。语义与 `evals/harness.py` 的 Expectation 一致。

    这里刻意只做「结构化的行为断言」，文本质量交给 rubric judge。两边都判同一件事
    会让一个错误被计两次分，也会让报告无法解释失败到底来自哪一侧。
    """

    model_config = ConfigDict(extra="forbid")

    tools: list[str] | None = None
    tools_include: list[str] = Field(default_factory=list)
    tools_exclude: list[str] = Field(default_factory=list)
    max_tool_calls: int | None = Field(default=None, ge=0)
    answer_contains: list[str] = Field(default_factory=list)
    answer_not_contains: list[str] = Field(default_factory=list)
    min_answer_chars: int | None = Field(default=None, ge=0)
    max_answer_chars: int | None = Field(default=None, ge=0)


class Task(BaseModel):
    """一条评测任务。`setup` 是给被测目标的环境说明（工具故障注入等）。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    category: Dimension
    prompt: str = Field(min_length=1)
    rubric: list[RubricItem] = Field(default_factory=list)
    gates: list[Gate] = Field(default_factory=list)
    expect: Expectation | None = None
    weight: float = Field(default=1.0, gt=0)
    #: 通过线（加权归一化分）。安全类任务通常给 1.0：安全没有「八成安全」。
    pass_threshold: float = Field(default=0.75, ge=0, le=1)
    #: 环境/工具前置说明，由 Target 解释，评测层不假设其结构。
    setup: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    #: 外部依赖缺失时该任务记 SKIP 而不是 FAIL。
    requires: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate(self) -> Task:
        if not self.rubric and self.expect is None and not self.gates:
            raise ValueError(f"任务 {self.id} 没有任何判分来源（rubric/expect/gates 全空），永远不会红")
        ids = [item.id for item in self.rubric]
        if len(ids) != len(set(ids)):
            raise ValueError(f"任务 {self.id} 的 rubric.id 重复：{sorted(ids)}")
        gate_ids = [gate.id for gate in self.gates]
        if len(gate_ids) != len(set(gate_ids)):
            raise ValueError(f"任务 {self.id} 的 gate.id 重复：{sorted(gate_ids)}")
        if self.category == "safety" and not self.gates:
            raise ValueError(f"安全任务 {self.id} 必须带 gate：安全项不能只靠加权分判通过")
        return self

    def fingerprint(self) -> str:
        return fingerprint(self.model_dump(mode="json"))


class TraceEvent(BaseModel):
    """轨迹事件。`at_ms` 相对本次执行开始，便于在报告里画时间线。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["message", "tool_call", "tool_result", "error", "guard"]
    name: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    at_ms: int = Field(default=0, ge=0)


class Trace(BaseModel):
    """一次执行的完整观察。判分器只能看它。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    sample: int = Field(default=0, ge=0)
    events: list[TraceEvent] = Field(default_factory=list)
    final_answer: str = ""
    error: str | None = None
    latency_ms: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    #: 被测系统的配置指纹（模型、提示词、工具集），用于报告复现。
    config_fingerprint: str = ""
    #: 被测系统自报的护栏/降级事件（例如工具失败降级），安全判定会读它。
    guard_events: list[str] = Field(default_factory=list)
    #: 环境注入的标记：security 探针把 canary 放在这里，判定器据此查泄露。
    canary: str | None = None

    @property
    def tool_calls(self) -> list[TraceEvent]:
        return [event for event in self.events if event.kind == "tool_call"]

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(event.name or "" for event in self.tool_calls)


class Score(BaseModel):
    """一条 rubric 的判分结果。value 已归一化到 0..1。"""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    dimension: Dimension
    value: float = Field(ge=0, le=1)
    rationale: str = ""
    source: JudgeKind = "llm"


class SampleVerdict(BaseModel):
    """单个样本的结论。passed 由 `quality.graders.base.judge` 计算，不在别处重算。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    sample: int = Field(ge=0)
    passed: bool
    score: float = Field(ge=0, le=1)
    scores: list[Score] = Field(default_factory=list)
    gates_failed: list[str] = Field(default_factory=list)
    #: 命中的断言失败原因（确定性侧），与 gates_failed 分开，便于区分「行为不对」与「不安全」。
    expectation_failures: list[str] = Field(default_factory=list)
    trace: Trace


class TaskSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    category: Dimension
    samples: int
    passed_samples: int
    score: float
    dimension_scores: dict[str, float] = Field(default_factory=dict)
    gates_failed: list[str] = Field(default_factory=list)
    failed_items: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0

    @property
    def pass_rate(self) -> float:
        return self.passed_samples / self.samples if self.samples else 0.0


class RunReport(BaseModel):
    """一次运行的完整报告。`metrics` 的定义见 `quality.runner.metrics`。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    suite_id: str
    suite_fingerprint: str
    target: str
    target_fingerprint: str
    config: dict[str, Any] = Field(default_factory=dict)
    samples: int = Field(ge=1)
    tasks: list[TaskSummary] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)
    totals: dict[str, float] = Field(default_factory=dict)
    duration_ms: int = 0
    cost_usd: float = 0.0
    audit_ref: str | None = None

    def task(self, task_id: str) -> TaskSummary | None:
        return next((item for item in self.tasks if item.task_id == task_id), None)


# =====================================================================
# 调优
# =====================================================================


class FailureCluster(BaseModel):
    """同类失败聚成一簇：调优的输入必须是「共因」，不是「一长串个案」。"""

    model_config = ConfigDict(extra="forbid")

    key: str
    dimension: Dimension
    task_ids: list[str] = Field(default_factory=list)
    count: int = Field(default=0, ge=0)
    sample_rationales: list[str] = Field(default_factory=list)


class Patch(BaseModel):
    """一处可回滚的改动。`diff` 是统一 diff 文本，便于人审与自动应用。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["prompt", "tool_description", "routing"]
    target_file: str
    diff: str
    rationale: str = ""
    parent_id: str | None = None


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    patch: Patch | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    cost_usd: float = 0.0
    passed: bool = False
    prompt_sha: str = ""


class TuningReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    base_run: str
    objective: str
    candidates: list[Candidate] = Field(default_factory=list)
    pareto_front: list[str] = Field(default_factory=list)
    selected: str | None = None
    stopped_reason: str = ""
    audit_ref: str | None = None


# =====================================================================
# 安全
# =====================================================================


class SecurityFinding(BaseModel):
    """一次成功的攻击（对防守方是漏洞，对评测方是 finding）。"""

    model_config = ConfigDict(extra="forbid")

    probe_id: str
    owasp: str
    severity: Severity
    task_id: str
    sample: int
    evidence: str = ""
    trace_ref: str = ""


class SecurityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    suite_id: str
    suite_fingerprint: str
    target: str
    probes: int = Field(ge=0)
    attempts: int = Field(ge=0)
    successes: int = Field(ge=0)
    by_owasp: dict[str, float] = Field(default_factory=dict)
    by_severity: dict[str, int] = Field(default_factory=dict)
    risk_score: float = Field(default=0.0, ge=0, le=100)
    findings: list[SecurityFinding] = Field(default_factory=list)
    audit_ref: str | None = None

    @property
    def attack_success_rate(self) -> float:
        return self.successes / self.attempts if self.attempts else 0.0


class AuditEntry(BaseModel):
    """append-only 审计行。企业里「谁在什么时候用哪套用例判了哪次发布」要能查。"""

    model_config = ConfigDict(extra="forbid")

    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    actor: str
    tenant: str
    action: str
    subject: str
    outcome: str = "ok"
    detail: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "DIMENSIONS",
    "SCORE_SCALE",
    "AuditEntry",
    "Candidate",
    "Dimension",
    "Expectation",
    "FailureCluster",
    "Gate",
    "Patch",
    "RubricItem",
    "RunReport",
    "SampleVerdict",
    "Score",
    "SecurityFinding",
    "SecurityReport",
    "Severity",
    "Task",
    "TaskSummary",
    "Trace",
    "TraceEvent",
    "TuningReport",
    "fingerprint",
]
