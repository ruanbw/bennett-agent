"""契约测试：判分器这一层实现了 `quality/INTERFACES.md` 里写死的那几个签名。

为什么值得单独测：这些函数被并行开发的其他子任务按签名 import（执行器调 `judge`、
门禁读 `SampleVerdict`、安全扫描读 `DETECTORS`）。签名漂移不会让任何单测变红，
只会让调用方在运行时拿到 `TypeError` 或——更糟——一个少传了 `expectation_failures`
却照样判过的结果。所以这里用 `inspect` 把签名与字段形状钉死，改契约必须先改这个文件。
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from evals.harness import Expectation as HarnessExpectation
from quality.graders.base import Grader, judge, weighted_score
from quality.graders.deterministic import ExpectationGrader
from quality.graders.rubric import JudgeClient, OpenJudgeClient, RubricJudge
from quality.graders.security import DETECTORS, evaluate_gates
from quality.models import DIMENSIONS, Expectation, SampleVerdict, Score, Task, Trace
from quality.suites.loader import Suite, load_suite, load_suites

QUALITY_DIR = Path(__file__).resolve().parent.parent
GRADERS_DIR = QUALITY_DIR / "graders"
SUITE_DIR = QUALITY_DIR / "suites"

#: INTERFACES.md「Detector 契约」里列出的 7 个名字。
DOCUMENTED_DETECTORS = (
    "canary_leak",
    "ssrf_attempt",
    "prompt_exfiltration",
    "destructive_command",
    "exfil_markdown",
    "jailbreak_compliance",
    "secret_pattern",
)


def signature_of(func: object) -> inspect.Signature:
    return inspect.signature(func)  # type: ignore[arg-type]


# =====================================================================
# 判分入口
# =====================================================================


def test_judge_signature_is_frozen():
    signature = signature_of(judge)
    assert list(signature.parameters) == ["task", "trace", "scores", "gates_failed", "expectation_failures"]
    for name in ("scores", "gates_failed", "expectation_failures"):
        parameter = signature.parameters[name]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, f"{name} 必须是 keyword-only"
        # 不给默认值：漏传 expectation_failures 应当立刻 TypeError，而不是被当成「没有失败」。
        assert parameter.default is inspect.Parameter.empty, f"{name} 不能有默认值"


def _task() -> Task:
    """一条最小的合法任务：models 要求至少有 rubric/expect/gates 之一。"""
    return Task(id="t", category="completion", prompt="p", expect=Expectation(min_answer_chars=1))


def _trace() -> Trace:
    return Trace(task_id="t")


def test_judge_rejects_a_call_without_expectation_failures():
    with pytest.raises(TypeError):
        judge(_task(), _trace(), scores=[], gates_failed=[])  # type: ignore[call-arg]


def test_weighted_score_signature_is_frozen():
    assert list(signature_of(weighted_score).parameters) == ["task", "scores"]


def test_judge_returns_the_frozen_verdict_shape():
    verdict = judge(_task(), _trace(), scores=[], gates_failed=[], expectation_failures=[])
    assert isinstance(verdict, SampleVerdict)
    assert set(SampleVerdict.model_fields) >= {
        "task_id",
        "sample",
        "passed",
        "score",
        "scores",
        "gates_failed",
        "expectation_failures",
        "trace",
    }


def test_score_values_are_normalized_by_the_model():
    with pytest.raises(ValueError):
        Score(item_id="a", dimension="completion", value=1.5)


def test_task_dimensions_come_from_the_frozen_list():
    suite = load_suite(SUITE_DIR / "core-behavior.yaml")
    assert {task.category for task in suite.tasks} <= set(DIMENSIONS)


# =====================================================================
# Grader 协议
# =====================================================================


class _GraderWithoutGrade:
    """有 id 但没有 grade：协议必须把它拒掉，否则执行器会在运行期 AttributeError。"""

    id = "half-baked"


def test_grader_protocol_members():
    assert getattr(Grader, "_is_protocol", False) is True
    assert isinstance(ExpectationGrader(), Grader)
    assert not isinstance(_GraderWithoutGrade(), Grader)


def test_shipped_graders_satisfy_the_protocol():
    assert isinstance(ExpectationGrader(), Grader)
    assert isinstance(RubricJudge(OpenJudgeClient()), Grader)


def test_grader_ids_are_distinct_and_match_judge_kinds():
    assert ExpectationGrader().id == "deterministic"
    assert RubricJudge(OpenJudgeClient()).id == "llm"


class _ClientWithOtherMethod:
    async def complete_text(self, system: str, user: str) -> str:
        return ""


def test_judge_client_protocol_is_a_single_method():
    assert getattr(JudgeClient, "_is_protocol", False) is True
    assert isinstance(OpenJudgeClient(), JudgeClient)
    assert not isinstance(_ClientWithOtherMethod(), JudgeClient)


# =====================================================================
# Detector 契约
# =====================================================================


def test_all_documented_detectors_are_implemented():
    missing = [name for name in DOCUMENTED_DETECTORS if name not in DETECTORS]
    assert not missing, f"契约里列出的 detector 没有实现：{missing}"


@pytest.mark.parametrize("name", DOCUMENTED_DETECTORS)
def test_detectors_take_trace_and_params(name: str):
    detector = DETECTORS[name]
    assert callable(detector)
    assert list(signature_of(detector).parameters) == ["trace", "params"]


def test_evaluate_gates_signature_is_frozen():
    assert list(signature_of(evaluate_gates).parameters) == ["task", "trace"]


def test_every_suite_gate_matches_a_detector():
    for suite in load_suites(SUITE_DIR):
        for task in suite.tasks:
            for gate in task.gates:
                assert gate.detector in DETECTORS, f"{suite.id}/{task.id}/{gate.id}"


# =====================================================================
# 用例装载契约
# =====================================================================


def test_suite_model_matches_interfaces():
    assert set(Suite.model_fields) == {"id", "version", "description", "tasks"}
    assert callable(Suite.fingerprint)


def test_loader_signatures_are_frozen():
    assert list(signature_of(load_suite).parameters) == ["path"]
    assert list(signature_of(load_suites).parameters) == ["root"]


def test_shipped_suite_ids_match_their_file_names():
    for path in sorted(SUITE_DIR.glob("*.yaml")):
        assert load_suite(path).id == path.stem


# =====================================================================
# 分层：判分器不许反向依赖执行器与被测系统
# =====================================================================


def test_graders_do_not_import_the_runner_targets_or_app():
    forbidden = ("quality.runner", "quality.targets", "quality.tuning", "quality.security", "app.")
    offenders: list[str] = []
    for path in sorted(GRADERS_DIR.glob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            if any(token in stripped for token in forbidden):
                offenders.append(f"{path.name}: {stripped}")
    assert not offenders, f"判分器不能依赖执行器/被测系统：{offenders}"


def test_expectation_fields_are_a_subset_of_the_harness_fields():
    # 确定性判分只是适配器：Task.expect 出现 harness 不认识的新字段时，这里会立刻红，
    # 而不是等到运行时被 pydantic 静默丢掉。
    assert set(Expectation.model_fields) <= set(HarnessExpectation.model_fields)
