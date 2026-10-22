"""分块策略的行为锁。

分块是检索质量的上限：切碎了取到半句话，切大了向量被平均掉。这里的用例锁住
**四条取舍**，而不是某个具体的字符数：

1. 结构优先（Markdown 标题与段落）、句子兜底，不硬切字符；
2. 每块都带「所属章节」，检索结果才能告诉用户「这句来自哪一节」；
3. 碎块要合并，标题行不能单独成块；
4. 块长受上限约束（重叠也算在内），否则调用方按上限估的预算全错。
"""

import pytest

from app.services.rag.chunking import (
    DEFAULT_CHUNK_CHARS,
    DEFAULT_OVERLAP_CHARS,
    MIN_CHUNK_CHARS,
    Chunk,
    chunk_text,
    pack_units,
    split_paragraphs,
    split_sections,
    split_sentences,
    truncate_to_budget,
)

LONG_BODY = "这是一段用于测试的正文，它需要在句子边界处被切开。" * 40


# ---------------------------------------------------------------------
# 结构切分
# ---------------------------------------------------------------------
def test_split_sections_keeps_heading_with_body():
    text = "# 退款政策\n\n7 天无理由。\n\n## 条件\n\n商品完好。\n"

    sections = split_sections(text)

    assert len(sections) == 2
    # 标题行保留在正文里：它是这一节最强的主题信号
    assert sections[0][0] == "退款政策"
    assert sections[0][1].startswith("# 退款政策")
    assert sections[1][0] == "条件"
    assert "商品完好" in sections[1][1]


def test_split_sections_with_leading_preamble():
    """第一个标题之前的引言标题为空串，不能丢掉。"""
    sections = split_sections("一段没有标题的引言。\n\n# 标题\n\n正文。\n")

    assert sections[0] == ("", "一段没有标题的引言。")
    assert sections[1][0] == "标题"


def test_split_sections_without_headings():
    sections = split_sections("只有正文，没有标题。\n\n第二段。\n")

    assert len(sections) == 1
    assert sections[0][0] == ""


def test_split_paragraphs_keeps_single_newlines():
    """代码块与列表依赖单换行；只有空行才是分段。"""
    paragraphs = split_paragraphs("第一行\n第二行\n\n第二段")

    assert paragraphs == ["第一行\n第二行", "第二段"]


def test_split_sentences_handles_chinese_and_english():
    assert split_sentences("第一句。第二句！第三句？") == ["第一句。", "第二句！", "第三句？"]
    assert split_sentences("One. Two! Three?") == ["One.", "Two!", "Three?"]


def test_split_sentences_does_not_break_decimals():
    """`3.14` 与 `v1.2` 不能在点号处切开——那会把一个数字变成两句话。"""
    pieces = split_sentences("圆周率约等于 3.14，版本 v1.2 已经发布。")

    assert len(pieces) == 1


# ---------------------------------------------------------------------
# 打包
# ---------------------------------------------------------------------
def test_pack_units_respects_limit():
    units = ["a" * 100, "b" * 100, "c" * 100]

    packed = pack_units(units, 250)

    assert all(len(block) <= 250 for block in packed)
    assert len(packed) == 2


def test_pack_units_keeps_oversized_unit_alone():
    """单个单元超限时独占一块，不硬切——硬切会把一句话劈成两半。"""
    packed = pack_units(["x" * 500], 100)

    assert packed == ["x" * 500]


# ---------------------------------------------------------------------
# 端到端分块
# ---------------------------------------------------------------------
def test_empty_document_yields_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_chunks_carry_their_heading():
    text = f"# 第一部分\n\n{LONG_BODY}\n\n# 第二部分\n\n{LONG_BODY}\n"

    chunks = chunk_text(text, max_chars=400, overlap_chars=60)

    headings = {chunk.heading for chunk in chunks}
    assert headings == {"第一部分", "第二部分"}
    assert all(chunk.heading for chunk in chunks)


def test_chunk_lengths_respect_the_limit():
    """重叠也算在块长里：调用方按 `max_chars` 估预算，超了就是估错。"""
    text = f"# 大标题\n\n{LONG_BODY}\n"

    chunks = chunk_text(text, max_chars=400, overlap_chars=80)

    assert chunks
    # 允许少量超出：拼重叠时按行连接会多一个换行符
    assert all(chunk.chars <= 400 + 2 for chunk in chunks), [chunk.chars for chunk in chunks]


def test_chunks_are_indexed_in_order():
    text = f"# 标题\n\n{LONG_BODY}\n"

    chunks = chunk_text(text, max_chars=300, overlap_chars=50)

    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))


def test_heading_only_block_is_merged():
    """`## 流程` 下一行就是下一个标题：这种块没有任何可检索内容。"""
    text = "# A\n\n内容一。\n\n## 空节\n\n## B\n\n内容二。\n"

    chunks = chunk_text(text, max_chars=800)

    # 「空节」不会独立成块（它的正文为空，只剩标题行，会被并进前一块）
    assert all(chunk.text.strip() != "## 空节" for chunk in chunks)


def test_tiny_leading_block_is_merged_forward():
    """以一两行引言开头的文档：首块没有「前一块」可并，要并进后一块。"""
    text = "引言。\n\n# 第一节\n\n" + LONG_BODY + "\n"

    chunks = chunk_text(text, max_chars=400, overlap_chars=50)

    assert len(chunks[0].text) >= MIN_CHUNK_CHARS


def test_overlap_carries_context_between_chunks():
    """相邻块之间要有重叠：答案跨在切点上时，后一块仍能读到前一块的结尾。"""
    text = "第一句话很重要。第二句话也很重要。第三句话同样重要。" * 30

    chunks = chunk_text(text, max_chars=300, overlap_chars=80)
    assert len(chunks) >= 2

    # 第二块的开头应当能在第一块里找到（重叠段）
    first_tail = chunks[0].text[-80:]
    second_head = chunks[1].text[:40]
    assert any(fragment in first_tail for fragment in [second_head[:20]] if fragment)


def test_zero_chunk_size_is_rejected():
    """0 会让分块退化成「每句一块」，是个静默的质量崩塌，不如直接拒绝。"""
    with pytest.raises(ValueError, match="max_chars"):
        chunk_text("一些正文", max_chars=0)


def test_overlap_is_clamped_below_half_chunk():
    """重叠大于等于块长时相邻块几乎完全重复，检索名额会被同一段内容占满。"""
    text = LONG_BODY

    chunks = chunk_text(text, max_chars=200, overlap_chars=500)
    assert chunks
    assert all(chunk.chars <= 200 + 2 for chunk in chunks)


def test_defaults_are_coherent():
    """默认值之间必须自洽：重叠要远小于块长，碎块阈值也要远小于块长。"""
    assert 0 < DEFAULT_OVERLAP_CHARS < DEFAULT_CHUNK_CHARS // 2
    assert 0 < MIN_CHUNK_CHARS < DEFAULT_CHUNK_CHARS // 4


# ---------------------------------------------------------------------
# 硬上限兜底
# ---------------------------------------------------------------------
def test_truncate_to_budget_keeps_head_and_tail():
    text = "头" * 100 + "中" * 500 + "尾" * 100

    result = truncate_to_budget(text, 200)

    assert len(result) <= 200 + 20
    assert result.startswith("头")
    assert result.endswith("尾")
    assert "已截断" in result


def test_truncate_to_budget_is_identity_below_limit():
    assert truncate_to_budget("短文本", 100) == "短文本"


def test_chunk_is_frozen():
    """不可变：块一旦入库，它的文本与归属章节就不该再被就地改写。"""
    from dataclasses import FrozenInstanceError

    chunk = Chunk(index=0, text="x", heading="h")
    with pytest.raises(FrozenInstanceError):
        chunk.text = "y"  # type: ignore[misc]


def test_split_sentences_does_not_break_domains():
    """`example.com` 里的点不是句子边界：切开会把一个域名变成两句话。"""
    assert split_sentences("访问 example.com 即可，然后点登录。") == ["访问 example.com 即可，然后点登录。"]


def test_split_sentences_keeps_punctuation():
    """标点留在前一段末尾：丢掉它，相邻两块拼起来会缺字。"""
    pieces = split_sentences("第一句。第二句。")

    assert pieces == ["第一句。", "第二句。"]
