"""用例装载测试：仓库里的真实用例必须能装载，写错的用例必须在装载期就红。

用例文件是评测的"数据面"，它的错误模式同样是静默的：gate detector 名字打错一个字母，
这条安全门禁就永久失效；`expect: {}` 这种空断言会让任务永远绿。所以这里的用例一半在
检查交付的 YAML，一半在检查装载器有没有把这类错误挡住。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from quality.graders.security import DETECTORS
from quality.suites.loader import Suite, load_suite, load_suites, parse_suite

SUITE_DIR = Path(__file__).resolve().parent.parent / "suites"

#: core-behavior.yaml 与 robustness.yaml 里用 tags 标出的、提示词真承诺过的行为。
#: 少一个就说明有承诺没有被任何用例覆盖（调优阶段会改不到它）。
REQUIRED_TAGS = frozenset({"time", "tools", "clarification", "language", "failure-report", "injection"})


def shipped() -> list[Suite]:
    return load_suites(SUITE_DIR)


def task_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "t1",
        "category": "completion",
        "prompt": "问题",
        "rubric": [{"id": "a", "dimension": "completion", "description": "判分项"}],
    }
    payload.update(overrides)
    return payload


def write_suite(tmp_path: Path, name: str, payload: dict[str, object]) -> Path:
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return path


# =====================================================================
# 交付的用例
# =====================================================================


def test_shipped_suites_load():
    suites = shipped()
    assert [suite.id for suite in suites] == ["core-behavior", "robustness"]
    assert all(suite.version == 1 for suite in suites)
    assert all(suite.description.strip() for suite in suites)


def test_shipped_suite_covers_twelve_to_eighteen_tasks():
    tasks = [task for suite in shipped() for task in suite.tasks]
    assert 12 <= len(tasks) <= 18


def test_task_ids_are_unique_across_suites():
    # 报告与基线按任务 id 对齐，跨套件重名会让两份套件的结果互相覆盖。
    ids = [task.id for suite in shipped() for task in suite.tasks]
    assert len(ids) == len(set(ids))


def test_every_shipped_task_has_a_judging_source():
    for suite in shipped():
        for task in suite.tasks:
            assert task.rubric or task.expect is not None or task.gates, task.id
            for rubric_item in task.rubric:
                # 判分项必须是可判定的行为描述，不能是「回答得好不好」这类空话。
                assert len(rubric_item.description) >= 10, f"{task.id}/{rubric_item.id} 描述太短，无法判定"


def test_safety_tasks_are_gated_and_use_full_threshold():
    for suite in shipped():
        for task in suite.tasks:
            if task.category != "safety":
                continue
            assert task.gates, f"{task.id} 是安全任务却没有 gate"
            assert task.pass_threshold == 1.0, f"{task.id} 的通过线必须为 1.0：安全没有八成安全"


def test_all_gate_detectors_are_implemented():
    for suite in shipped():
        for task in suite.tasks:
            for gate in task.gates:
                assert gate.detector in DETECTORS, f"{task.id}/{gate.id} 引用了未知 detector {gate.detector}"


def test_required_behaviors_are_covered():
    tags = {tag for suite in shipped() for task in suite.tasks for tag in task.tags}
    missing = sorted(REQUIRED_TAGS - tags)
    assert not missing, f"这些承诺没有被任何用例覆盖：{missing}"


def test_prompts_are_distinct():
    prompts = [task.prompt for suite in shipped() for task in suite.tasks]
    assert len(prompts) == len(set(prompts))


def test_shipped_fingerprints_are_stable_and_distinct():
    first = {suite.id: suite.fingerprint() for suite in shipped()}
    second = {suite.id: suite.fingerprint() for suite in shipped()}
    assert first == second
    assert len(set(first.values())) == len(first)


def test_fingerprint_changes_when_a_task_changes():
    suite = shipped()[0]
    payload = suite.model_dump(mode="json")
    payload["suite"] = payload.pop("id")
    before = parse_suite(payload, source=Path("core-behavior.yaml")).fingerprint()
    payload["tasks"][0]["prompt"] = payload["tasks"][0]["prompt"] + "（改过）"
    after = parse_suite(payload, source=Path("core-behavior.yaml")).fingerprint()
    assert before != after


# =====================================================================
# 装载器的拒绝行为
# =====================================================================


def test_load_suite_reads_a_valid_file(tmp_path: Path):
    path = write_suite(tmp_path, "demo", {"suite": "demo", "version": 2, "description": "d", "tasks": [task_payload()]})
    suite = load_suite(path)
    assert suite.id == "demo"
    assert suite.version == 2
    assert suite.tasks[0].id == "t1"


def test_suite_id_must_match_filename(tmp_path: Path):
    path = write_suite(tmp_path, "wrong-name", {"suite": "demo", "version": 1, "tasks": [task_payload()]})
    with pytest.raises(ValueError, match="必须与文件名"):
        load_suite(path)


def test_missing_file_reports_the_path(tmp_path: Path):
    with pytest.raises(ValueError, match="用例文件不存在"):
        load_suite(tmp_path / "nope.yaml")


def test_invalid_yaml_reports_the_file(tmp_path: Path):
    path = tmp_path / "demo.yaml"
    path.write_text("suite: [unclosed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="不是合法 YAML"):
        load_suite(path)


def test_unknown_top_level_key_is_rejected(tmp_path: Path):
    # `task:` 与 `tasks:` 只差一个字母，静默忽略会让整份文件变成空套件。
    path = write_suite(tmp_path, "demo", {"suite": "demo", "version": 1, "task": [task_payload()]})
    with pytest.raises(ValueError, match="未知键"):
        load_suite(path)


def test_empty_tasks_is_rejected(tmp_path: Path):
    path = write_suite(tmp_path, "demo", {"suite": "demo", "version": 1, "tasks": []})
    with pytest.raises(ValueError, match="非空列表"):
        load_suite(path)


def test_duplicate_task_ids_are_rejected(tmp_path: Path):
    path = write_suite(tmp_path, "demo", {"suite": "demo", "version": 1, "tasks": [task_payload(), task_payload()]})
    with pytest.raises(ValueError, match="任务 id 重复"):
        load_suite(path)


def test_unknown_detector_is_rejected_with_file_and_task(tmp_path: Path):
    payload = {
        "suite": "demo",
        "version": 1,
        "tasks": [
            task_payload(
                category="safety",
                pass_threshold=1.0,
                gates=[{"id": "no_leak", "detector": "canary_leek", "params": {}}],
            ),
        ],
    }
    path = write_suite(tmp_path, "demo", payload)
    with pytest.raises(ValueError) as excinfo:
        load_suite(path)
    message = str(excinfo.value)
    assert "demo.yaml" in message
    assert "t1" in message
    assert "canary_leek" in message


def test_task_without_judging_source_is_rejected(tmp_path: Path):
    payload = {"suite": "demo", "version": 1, "tasks": [{"id": "t1", "category": "completion", "prompt": "问题"}]}
    path = write_suite(tmp_path, "demo", payload)
    with pytest.raises(ValueError, match="t1"):
        load_suite(path)


def test_empty_expect_is_rejected(tmp_path: Path):
    payload = {"suite": "demo", "version": 1, "tasks": [task_payload(rubric=[], expect={})]}
    path = write_suite(tmp_path, "demo", payload)
    with pytest.raises(ValueError, match="没有任何断言"):
        load_suite(path)


def test_safety_task_without_gate_is_rejected_by_the_model(tmp_path: Path):
    payload = {"suite": "demo", "version": 1, "tasks": [task_payload(category="safety", pass_threshold=1.0)]}
    path = write_suite(tmp_path, "demo", payload)
    with pytest.raises(ValueError, match="必须带 gate"):
        load_suite(path)


def test_load_suites_requires_a_directory(tmp_path: Path):
    with pytest.raises(ValueError, match="用例目录不存在"):
        load_suites(tmp_path / "missing")


def test_load_suites_rejects_empty_directory(tmp_path: Path):
    with pytest.raises(ValueError, match="没有 .*yaml"):
        load_suites(tmp_path)


def test_load_suites_sorts_by_filename(tmp_path: Path):
    write_suite(tmp_path, "b-suite", {"suite": "b-suite", "version": 1, "tasks": [task_payload()]})
    write_suite(tmp_path, "a-suite", {"suite": "a-suite", "version": 1, "tasks": [task_payload()]})
    assert [suite.id for suite in load_suites(tmp_path)] == ["a-suite", "b-suite"]
