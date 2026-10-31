"""任务清单工具：让模型把多步任务拆成可跟踪的待办，并在执行过程中更新状态。

设计取舍：

- **整表替换**：每次调用都传完整清单，服务端不做增量合并。模型与用户看到的始终是同一份
  快照，避免「只改了一项、其余项被悄悄丢掉」这类半更新状态。
- **无服务端状态**：清单以对话历史为载体。本服务端无状态、每轮由客户端回放历史，工具调用
  的参数与结果会一起回来；因此工具不读写数据库，也不需要 user_id / session_id。
- **只读风险等级**：它只校验并渲染模型自己提交的清单，没有外部副作用。若标成 SAFE_WRITE，
  每次更新计划都会弹一次审批，规划工具就不可用了。

状态与 DSH 的 `todo_write` 对齐：pending / in_progress / completed，同一时间最多一项进行中。
"""

import json
from collections import Counter
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import RunContext

TodoStatus = Literal["pending", "in_progress", "completed"]

#: 合法状态。只有三个值，线性成员判断足够；声明顺序与错误文案里的列举顺序一致。
TODO_STATUSES: tuple[TodoStatus, ...] = ("pending", "in_progress", "completed")

#: 清单上限：每次调用都会把完整清单写进参数与结果，任务越多 token 成本越高；20 项足够覆盖
#: 一次多步任务，超出时模型应合并步骤而不是继续堆。
MAX_TODOS = 20

#: 单条任务内容上限：清单是给人和模型看的短句，不是文档。超限拒绝而非截断——半截任务名
#: 会让后续轮次无法判断它到底指哪件事。
MAX_TODO_CONTENT_CHARS = 200

_STATUS_CHECKBOX = {
    "pending": " ",
    "in_progress": ">",
    "completed": "x",
}


class TodoItem(BaseModel):
    """一条任务。

    `extra="forbid"` 让模型多传字段时在 schema 边界失败，而不是静默丢掉——工具参数与
    回灌给模型的快照必须完全一致，否则模型会以为自己写入了某个字段。
    """

    model_config = ConfigDict(extra="forbid")

    content: str = Field(description="任务内容：一句可执行的短句，不要写成长段落。")
    status: TodoStatus = Field(
        description="pending（未开始）/ in_progress（进行中）/ completed（已完成）。",
    )


@dataclass(frozen=True, slots=True)
class NormalizedTodo:
    """通过校验后的任务；只有 `normalize_todos` 能构造出它。"""

    content: str
    status: TodoStatus


def _read_field(item: object, name: str) -> object:
    """同时接受 `TodoItem` 与普通 dict，方便直接调用与测试。"""
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _normalize_content(value: object) -> str | None:
    """把任务内容折叠成单行：多行内容会破坏清单渲染，也会把日志/提示词切成多段。"""
    if not isinstance(value, str):
        return None
    return " ".join(value.split())


def normalize_todos(todos: object) -> list[NormalizedTodo] | str:
    """校验并规范化任务列表：通过返回列表，不通过返回给模型看的错误文案。

    返回文案而非抛异常——工具失败不该中止整轮，模型看到原因可以自己改参数。顺序有意义：
    类型 → 数量 → 逐项（内容 → 重复 → 状态）→ 进行中数量。
    """
    if not isinstance(todos, (list, tuple)):
        return "错误: 任务列表 (todos) 必须为数组。"

    if len(todos) > MAX_TODOS:
        return f"错误: 任务数量 (todos) 不能超过 {MAX_TODOS} 项。"

    normalized: list[NormalizedTodo] = []
    seen: set[str] = set()
    active = 0

    for index, item in enumerate(todos, start=1):
        content = _normalize_content(_read_field(item, "content"))
        if not content:
            return f"错误: 第 {index} 项任务内容 (content) 不能为空。"
        if len(content) > MAX_TODO_CONTENT_CHARS:
            return f"错误: 第 {index} 项任务内容 (content) 不能超过 {MAX_TODO_CONTENT_CHARS} 字符。"
        if content in seen:
            return f"错误: 任务内容重复：{json.dumps(content, ensure_ascii=False)}。"

        status = _read_field(item, "status")
        if status not in TODO_STATUSES:
            return f"错误: 第 {index} 项任务状态 (status) 必须是 pending、in_progress 或 completed。"
        if status == "in_progress":
            active += 1

        seen.add(content)
        normalized.append(NormalizedTodo(content=content, status=cast(TodoStatus, status)))

    if active > 1:
        return "错误: 同一时间最多只能有 1 项任务处于 in_progress。"

    return normalized


def render_todos(todos: list[NormalizedTodo]) -> str:
    """把清单渲染成给模型和用户看的文本；空清单明确说「已清空」，不留下空段落。"""
    if not todos:
        return "任务清单已清空（0 项）。"

    counts = Counter(todo.status for todo in todos)
    lines = [
        f"任务清单已更新：共 {len(todos)} 项"
        f"（待办 {counts['pending']}，进行中 {counts['in_progress']}，已完成 {counts['completed']}）。"
    ]
    lines.extend(f"- [{_STATUS_CHECKBOX[todo.status]}] {todo.content}" for todo in todos)
    return "\n".join(lines)


async def todo_write(ctx: RunContext[None], todos: list[TodoItem]) -> str:
    """更新当前任务清单（整表替换），用于规划多步工作并展示进度。

    每次调用都必须传入**完整**的任务列表：新列表会替换旧列表，不是增量追加。
    只有在任务确实需要多个步骤时才使用；一步就能完成的简单任务不要为了走流程而建清单。
    开始一项任务前把它标为 in_progress，完成后立即标为 completed；同一时间最多一项 in_progress。

    Args:
        todos: 完整任务列表；每项包含 content（一句可执行的短句）与 status
            （pending / in_progress / completed）。传空数组表示清空清单。
    """
    normalized = normalize_todos(todos)
    if isinstance(normalized, str):
        return normalized
    return render_todos(normalized)


__all__ = [
    "MAX_TODO_CONTENT_CHARS",
    "MAX_TODOS",
    "TODO_STATUSES",
    "NormalizedTodo",
    "TodoItem",
    "TodoStatus",
    "normalize_todos",
    "render_todos",
    "todo_write",
]
