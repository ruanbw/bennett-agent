"""任务清单工具：整表替换、状态校验与只读装配。"""

import pytest

from app.tools import RiskLevel, ToolCategory, build_tools, get_registry
from app.tools.approval import requires_approval
from app.tools.todo import (
    MAX_TODO_CONTENT_CHARS,
    MAX_TODOS,
    TodoItem,
    normalize_todos,
    render_todos,
    todo_write,
)


@pytest.mark.asyncio
async def test_todo_write_returns_the_canonical_list():
    result = await todo_write(
        None,
        todos=[
            TodoItem(content="  调研 现有工具  ", status="completed"),
            TodoItem(content="实现 todo_write", status="in_progress"),
            TodoItem(content="补测试", status="pending"),
        ],
    )

    assert "任务清单已更新：共 3 项（待办 1，进行中 1，已完成 1）。" in result
    assert "- [x] 调研 现有工具" in result
    assert "- [>] 实现 todo_write" in result
    assert "- [ ] 补测试" in result


@pytest.mark.asyncio
async def test_empty_list_clears_the_list():
    assert await todo_write(None, todos=[]) == "任务清单已清空（0 项）。"


@pytest.mark.asyncio
async def test_empty_content_is_rejected():
    result = await todo_write(None, todos=[{"content": "  ", "status": "pending"}])  # type: ignore[list-item]

    assert result == "错误: 第 1 项任务内容 (content) 不能为空。"


@pytest.mark.asyncio
async def test_content_is_collapsed_to_one_line():
    result = await todo_write(
        None,
        todos=[{"content": "  第一行\n第二行\t  ", "status": "pending"}],  # type: ignore[list-item]
    )

    assert "- [ ] 第一行 第二行" in result
    assert "\n第二行" not in result


@pytest.mark.asyncio
async def test_duplicate_content_is_rejected_after_trimming():
    result = await todo_write(
        None,
        todos=[
            {"content": "同一项", "status": "pending"},
            {"content": " 同一项 ", "status": "completed"},
        ],  # type: ignore[list-item]
    )

    assert "任务内容重复" in result
    assert '"同一项"' in result


@pytest.mark.asyncio
async def test_more_than_one_in_progress_is_rejected():
    result = await todo_write(
        None,
        todos=[
            {"content": "A", "status": "in_progress"},
            {"content": "B", "status": "in_progress"},
        ],  # type: ignore[list-item]
    )

    assert "最多只能有 1 项任务处于 in_progress" in result


@pytest.mark.asyncio
async def test_unknown_status_is_rejected():
    result = await todo_write(None, todos=[{"content": "A", "status": "doing"}])  # type: ignore[list-item]

    assert "必须是 pending、in_progress 或 completed" in result


def test_non_list_is_rejected():
    assert normalize_todos(None) == "错误: 任务列表 (todos) 必须为数组。"


def test_too_many_todos_are_rejected():
    todos = [{"content": f"任务{i}", "status": "pending"} for i in range(MAX_TODOS + 1)]

    assert normalize_todos(todos) == f"错误: 任务数量 (todos) 不能超过 {MAX_TODOS} 项。"


def test_max_todos_at_limit_is_accepted():
    todos = [{"content": f"任务{i}", "status": "pending"} for i in range(MAX_TODOS)]

    normalized = normalize_todos(todos)

    assert not isinstance(normalized, str)
    assert len(normalized) == MAX_TODOS


def test_content_at_limit_is_accepted():
    content = "任" * MAX_TODO_CONTENT_CHARS

    normalized = normalize_todos([{"content": content, "status": "pending"}])

    assert not isinstance(normalized, str)
    assert normalized[0].content == content


def test_content_over_limit_is_rejected():
    content = "任" * (MAX_TODO_CONTENT_CHARS + 1)

    assert normalize_todos([{"content": content, "status": "pending"}]) == (
        f"错误: 第 1 项任务内容 (content) 不能超过 {MAX_TODO_CONTENT_CHARS} 字符。"
    )


def test_all_completed_is_accepted():
    """收尾阶段没有进行中项是合法状态；只有「同时多个进行中」才需要拒绝。"""
    normalized = normalize_todos(
        [
            {"content": "A", "status": "completed"},
            {"content": "B", "status": "completed"},
        ]
    )

    assert not isinstance(normalized, str)
    assert render_todos(normalized) == "任务清单已更新：共 2 项（待办 0，进行中 0，已完成 2）。\n- [x] A\n- [x] B"


def test_registered_as_a_read_only_planning_tool():
    tool = get_registry().get("todo_write")

    assert tool is not None
    assert tool.category == ToolCategory.SYSTEM
    assert tool.risk_level == RiskLevel.READ_ONLY
    assert tool.permissions == ()
    assert tool.metadata["permissions"] == []
    assert "planning" in tool.tags
    assert tool.timeout is None
    assert tool.is_enabled
    assert requires_approval(tool.risk_level, required_permissions=tool.permissions) is False


def test_build_tools_includes_todo_write():
    assert "todo_write" in {tool.name for tool in build_tools()}


def test_schema_tells_the_model_the_list_is_required_and_complete():
    tool = get_registry().get("todo_write")
    assert tool is not None

    schema = tool.function_schema.json_schema
    assert schema["required"] == ["todos"]
    item_schema = schema["$defs"]["TodoItem"]
    assert item_schema["additionalProperties"] is False
    assert item_schema["properties"]["status"]["enum"] == ["pending", "in_progress", "completed"]
    assert "完整" in schema["properties"]["todos"]["description"]
