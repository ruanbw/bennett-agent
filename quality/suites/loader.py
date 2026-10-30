"""用例装载：YAML → `Suite`。

装载期是所有校验该发生的地方。用例文件写错（gate detector 名字打错、任务 id 重复、
`expect: {}` 这种空断言）如果留到判分期，表现会是「这条任务永远绿」——比报错难查得多。
所以这里的口径是：**能在装载期发现的问题，一律抛 ValueError，并且文案必须带文件名与任务
id**，让人不用打开代码就知道哪一行要改。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from quality.graders.security import DETECTORS
from quality.models import Task
from quality.models import fingerprint as content_fingerprint

__all__ = ["Suite", "load_suite", "load_suites", "parse_suite"]

#: 用例文件允许的顶层键。多出来的键几乎都是笔误（`task:` vs `tasks:`），静默忽略等于
#: 让整份文件变成空套件。
_TOP_LEVEL_KEYS = frozenset({"suite", "version", "description", "tasks"})


class Suite(BaseModel):
    """一份用例集。`fingerprint` 用来回答「这次报告跑的是哪一版用例」。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    version: int = Field(ge=1)
    description: str = ""
    tasks: list[Task] = Field(min_length=1)

    def fingerprint(self) -> str:
        """内容指纹：任务内容或顺序变了就变。

        顺序也算进去是刻意的：报告里的任务顺序、调优时的对照都按这个顺序读，
        换序后仍然用同一份基线会让 diff 变得难解释。
        """
        return content_fingerprint(
            {"suite": self.id, "version": self.version, "tasks": [task.fingerprint() for task in self.tasks]},
        )


def _require_supported_gates(task: Task, source: Path) -> None:
    for gate in task.gates:
        if gate.detector not in DETECTORS:
            raise ValueError(
                f"{source}：任务 {task.id!r} 的 gate {gate.id!r} 引用了未知 detector {gate.detector!r}，"
                f"可选：{sorted(DETECTORS)}",
            )


def _require_assertions(task: Task, source: Path) -> None:
    if task.expect is not None and not (
        task.expect.tools is not None
        or task.expect.tools_include
        or task.expect.tools_exclude
        or task.expect.max_tool_calls is not None
        or task.expect.answer_contains
        or task.expect.answer_not_contains
        or task.expect.min_answer_chars is not None
        or task.expect.max_answer_chars is not None
    ):
        raise ValueError(f"{source}：任务 {task.id!r} 的 expect 没有任何断言，等于永远不会红")


def parse_suite(payload: Any, *, source: Path = Path("<memory>")) -> Suite:
    """把 YAML 解析出来的结构转成 `Suite`。`source` 只用于错误文案。"""
    if not isinstance(payload, dict):
        raise ValueError(f"{source}：用例文件顶层必须是映射，且含 suite/version/tasks")

    unknown = set(payload) - _TOP_LEVEL_KEYS
    if unknown:
        raise ValueError(f"{source}：顶层出现未知键 {sorted(unknown)}，可选：{sorted(_TOP_LEVEL_KEYS)}")

    suite_id = payload.get("suite")
    if not isinstance(suite_id, str) or not suite_id:
        raise ValueError(f"{source}：缺少 suite（用例集 id）")

    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError(f"{source}：tasks 必须是非空列表")

    tasks: list[Task] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_tasks, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"{source}：第 {index} 条任务必须是映射")
        try:
            task = Task.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(f"{source}：第 {index} 条任务（id={raw.get('id')!r}）校验失败：{exc}") from exc
        if task.id in seen:
            raise ValueError(f"{source}：任务 id 重复 {task.id!r}")
        seen.add(task.id)
        _require_supported_gates(task, source)
        _require_assertions(task, source)
        tasks.append(task)

    try:
        return Suite(
            id=suite_id, version=payload.get("version", 1), description=payload.get("description") or "", tasks=tasks
        )
    except ValidationError as exc:
        raise ValueError(f"{source}：用例集元信息校验失败（suite={suite_id!r}）：{exc}") from exc


def load_suite(path: str | Path) -> Suite:
    """读一个 YAML 用例文件。`suite` 必须与文件名一致，否则并行加用例时很难确认跑的是哪份。"""
    file_path = Path(path)
    try:
        payload = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"用例文件不存在：{file_path}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"用例文件不是合法 YAML（{file_path}）：{exc}") from exc

    suite = parse_suite(payload, source=file_path)
    if suite.id != file_path.stem:
        raise ValueError(f"{file_path}：suite 名 {suite.id!r} 必须与文件名 {file_path.stem!r} 一致")
    return suite


def load_suites(root: str | Path) -> list[Suite]:
    """加载目录下全部 `*.yaml`，按文件名排序（报告与基线的顺序必须稳定）。"""
    directory = Path(root)
    if not directory.is_dir():
        raise ValueError(f"用例目录不存在：{directory}")
    paths = sorted(directory.glob("*.yaml"))
    if not paths:
        raise ValueError(f"用例目录下没有 *.yaml：{directory}")

    suites = [load_suite(path) for path in paths]
    ids = [suite.id for suite in suites]
    duplicated = sorted({suite_id for suite_id in ids if ids.count(suite_id) > 1})
    if duplicated:
        raise ValueError(f"用例目录 {directory} 下出现重复的 suite 名：{duplicated}")
    return suites
