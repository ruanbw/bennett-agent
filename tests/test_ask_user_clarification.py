"""用户意图澄清/提问工具测试。"""

import pytest

from app.tools.ask_user_clarification import ask_user_clarification


@pytest.mark.asyncio
async def test_ask_user_clarification_valid():
    result = await ask_user_clarification(
        None,
        question="您想要哪种组件？",
        options=["React", "Vue", "Angular"],
    )
    assert "已向用户发起意图确认提问：您想要哪种组件？" in result
    assert '"React", "Vue", "Angular"' in result


@pytest.mark.asyncio
async def test_ask_user_clarification_empty_question():
    result = await ask_user_clarification(
        None,
        question="  ",
        options=["React", "Vue"],
    )
    assert "错误: 提问内容 (question) 不能为空。" in result


@pytest.mark.asyncio
async def test_ask_user_clarification_invalid_options_type():
    assert "必须为列表或元组" in await ask_user_clarification(None, "问题？", None)  # type: ignore
    assert "必须为列表或元组" in await ask_user_clarification(None, "问题？", "选项")  # type: ignore
    assert "必须为列表或元组" in await ask_user_clarification(None, "问题？", 123)  # type: ignore


@pytest.mark.asyncio
async def test_ask_user_clarification_cleans_and_deduplicates_options():
    result = await ask_user_clarification(
        None,
        question="问题？",
        options=["React", "React", "  Vue  ", "", None, 123],  # type: ignore
    )
    assert '"React", "Vue"' in result


@pytest.mark.asyncio
async def test_ask_user_clarification_too_few_options():
    result = await ask_user_clarification(
        None,
        question="问题？",
        options=["React", "React", "  "],
    )
    assert "必须至少包含 2 个不同的非空选项" in result
