"""用户意图澄清/提问工具测试。"""

import pytest

from app.tools.ask_user_clarification import (
    MAX_OPTION_CHARS,
    MAX_OPTIONS,
    MAX_QUESTION_CHARS,
    ask_user_clarification,
)


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
async def test_ask_user_clarification_non_string_question_does_not_raise():
    """参数类型不对时返回错误文案，不能让 AttributeError 逃出工具。"""
    result = await ask_user_clarification(None, 123, ["React", "Vue"])  # type: ignore

    assert result.startswith("错误: 提问内容 (question)")


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


# ---------------------------------------------------------------------
# 输入上限（供模型自我约束；超限返回错误文案而不是抛异常）
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_question_at_limit_is_accepted():
    question = "问" * MAX_QUESTION_CHARS

    result = await ask_user_clarification(None, question, ["A", "B"])

    assert result.startswith("已向用户发起意图确认提问")


@pytest.mark.asyncio
async def test_question_over_limit_is_rejected():
    result = await ask_user_clarification(None, "问" * (MAX_QUESTION_CHARS + 1), ["A", "B"])

    assert result == f"错误: 提问内容 (question) 不能超过 {MAX_QUESTION_CHARS} 字符。"


@pytest.mark.asyncio
async def test_question_limit_ignores_surrounding_whitespace():
    """上限按 strip 后的长度算：模型容易被提示词里的换行坑到。"""
    result = await ask_user_clarification(None, "  " + "问" * MAX_QUESTION_CHARS + "  ", ["A", "B"])

    assert result.startswith("已向用户发起意图确认提问")


@pytest.mark.asyncio
async def test_max_options_is_accepted():
    options = [f"选项{i}" for i in range(MAX_OPTIONS)]

    result = await ask_user_clarification(None, "问题？", options)

    assert f'"选项{MAX_OPTIONS - 1}"' in result


@pytest.mark.asyncio
async def test_too_many_options_are_rejected():
    options = [f"选项{i}" for i in range(MAX_OPTIONS + 1)]

    result = await ask_user_clarification(None, "问题？", options)

    assert result == f"错误: 候选项数量 (options) 不能超过 {MAX_OPTIONS} 个。"


@pytest.mark.asyncio
async def test_duplicate_options_do_not_count_towards_the_limit():
    """上限按去重后的实际选项数算：重复项不会渲染给用户。"""
    options = [f"选项{i}" for i in range(MAX_OPTIONS)] + ["选项0", "选项1"]

    result = await ask_user_clarification(None, "问题？", options)

    assert result.startswith("已向用户发起意图确认提问")


@pytest.mark.asyncio
async def test_option_at_limit_is_accepted():
    long_option = "选" * MAX_OPTION_CHARS

    result = await ask_user_clarification(None, "问题？", [long_option, "B"])

    assert long_option in result


@pytest.mark.asyncio
async def test_option_over_limit_is_rejected():
    result = await ask_user_clarification(None, "问题？", ["选" * (MAX_OPTION_CHARS + 1), "B"])

    assert result == f"错误: 单个候选项 (option) 不能超过 {MAX_OPTION_CHARS} 字符。"


# ---------------------------------------------------------------------
# allow_custom_input 必须如实反映在回灌给模型的文案里
#
# 文案不分情况地说「可以选择或输入」时，模型会以为用户仍能自由补充，于是把问题写得
# 更开放；而前端在 allow_custom_input=False 时只显示选项。模型对交互形态的判断与
# 用户实际看到的不一致，后续轮次它会基于错误前提继续。
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_custom_input_allowed_is_stated():
    result = await ask_user_clarification(None, "问题？", ["A", "B"], allow_custom_input=True)

    assert "自行输入" in result


@pytest.mark.asyncio
async def test_custom_input_disallowed_is_stated():
    result = await ask_user_clarification(None, "问题？", ["A", "B"], allow_custom_input=False)

    assert "不允许自定义输入" in result
    assert "自行输入" not in result


# ---------------------------------------------------------------------
# normalize_clarification 是唯一实现（chat_service 的翻译层直接复用它）
# ---------------------------------------------------------------------
def test_normalize_returns_cleaned_args():
    from app.tools.ask_user_clarification import normalize_clarification

    normalized = normalize_clarification("  选哪个？  ", ["  React  ", "React", "Vue", "  ", ""])

    assert not isinstance(normalized, str)
    assert normalized.question == "选哪个？"
    # strip + 去重并保持原顺序
    assert normalized.options == ["React", "Vue"]


def test_normalize_drops_non_string_options():
    """模型偶尔会传数字/None；丢弃它们而不是整条拒绝，否则一次手误就废掉一个可用提问。"""
    from app.tools.ask_user_clarification import normalize_clarification

    normalized = normalize_clarification("问题？", ["A", 1, None, "B", {"x": 1}])

    assert not isinstance(normalized, str)
    assert normalized.options == ["A", "B"]


def test_normalize_is_frozen():
    """返回值不可变：翻译层与工具共享同一个对象时，任何一方都改不动。"""
    import dataclasses

    from app.tools.ask_user_clarification import normalize_clarification

    normalized = normalize_clarification("问题？", ["A", "B"])

    assert not isinstance(normalized, str)
    assert dataclasses.is_dataclass(normalized)
    with pytest.raises(dataclasses.FrozenInstanceError):
        normalized.question = "改掉"
