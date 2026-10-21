"""会话标题清洗与截断（`app/services/title_text.py`）。

标题是模型输出，天然带噪音：引号、「标题：」前缀、尾随句号、多行内容。这些如果不清洗
就会原样出现在侧边栏；而截断口径还要与前端 `clampTitle` 一致，否则同一个标题在两端
长度不同。
"""

import pytest

from app.services.title_text import clamp_title, fallback_title, normalize_title

MAX = 20


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("向量数据库选型", "向量数据库选型"),
        ('"向量数据库选型"', "向量数据库选型"),
        ("「向量数据库选型」", "向量数据库选型"),
        ("《向量数据库选型》", "向量数据库选型"),
        # 成对包裹嵌套时反复剥
        ('「"向量数据库选型"」', "向量数据库选型"),
    ],
)
def test_strips_wrapping_quotes(raw, expected):
    assert normalize_title(raw, MAX) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("标题：向量数据库选型", "向量数据库选型"),
        ("标题: 向量数据库选型", "向量数据库选型"),
        ("会话标题：向量数据库选型", "向量数据库选型"),
        ("TITLE: 向量数据库选型", "向量数据库选型"),
        ("主题：向量数据库选型", "向量数据库选型"),
    ],
)
def test_strips_model_added_prefixes(raw, expected):
    assert normalize_title(raw, MAX) == expected


def test_collapses_newlines_and_trailing_punctuation():
    """标题是单行文本：换行会被侧边栏截成半截，尾随句号是模型的口头习惯。"""
    assert normalize_title("向量数据库\n选型。", MAX) == "向量数据库 选型"
    assert normalize_title("  向量   检索  ", MAX) == "向量 检索"
    # 问号是语义的一部分，不能剥
    assert normalize_title("用哪个向量库？", MAX) == "用哪个向量库？"


def test_empty_or_noise_only_output_returns_empty_string():
    """清洗后为空要返回空串，由调用方决定兜底，而不是存一个「」进侧边栏。"""
    assert normalize_title(None, MAX) == ""
    assert normalize_title("", MAX) == ""
    assert normalize_title("   ", MAX) == ""
    assert normalize_title('""', MAX) == ""
    assert normalize_title("标题：", MAX) == ""


def test_clamp_counts_code_points_not_utf16_units():
    """emoji 不能被劈开，长度按码点算（与前端 clampTitle 同口径）。"""
    raw = "😀" * 25
    clamped = clamp_title(raw, MAX)

    assert clamped == "😀" * MAX
    assert len(clamped) == MAX


def test_clamp_keeps_short_titles_untouched():
    assert clamp_title("短标题", MAX) == "短标题"
    assert clamp_title("", MAX) == ""


def test_fallback_takes_first_line_and_drops_markdown_heading():
    """用户第一条消息常常直接是 Markdown，占位标题不该带着 `#`。"""
    assert fallback_title("# 帮我写个函数\n\n要求：可读", 15) == "帮我写个函数"
    assert fallback_title("  多行\n第二行", 15) == "多行"
    assert fallback_title("", 15) == ""


def test_fallback_clamps_by_code_points():
    assert fallback_title("长" * 40, 15) == "长" * 15
