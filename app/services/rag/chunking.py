"""文档分块：把一篇长文切成可检索的片段。

## 为什么分块策略必须显式设计

检索的质量上限由分块决定：切得太碎，一个概念被拆到三块里，哪一块都答不了完整问题；
切得太大，一块里混着五六个主题，向量被平均掉，召回变差、送进上下文也挤占预算。

这里用**结构优先、窗口兜底**的两级策略：

1. **先按结构切**：Markdown 标题（`#`）与空行分段。作者已经用格式表达了「哪些内容
   属于同一个话题」，这比任何固定长度切分都准。
2. **再按窗口切**：单个段落仍超过上限时，按句末标点切成句子再累积——不硬切字符，
   那会把一句话劈成两半，token 层面的语义直接断掉。
3. **块间重叠**：相邻块共享尾部若干字符。切点落在句子中间时，重叠让「上一块的结尾」
   在下一块里仍然完整，避免「问题答案正好跨在切点上」。

## 为什么不用固定字符数切分

固定长度（例如每 500 字符）在中文上尤其糟：它不认识句子边界，也不认识代码块。一篇
用 `##` 分好段的文档会被切成「前半段属于标题 A、后半段属于标题 B」的块，而检索时
两边的相似度都不够高。

## 边界

- 空文档、纯空白、只有标题的文档都返回空列表——调用方据此给出「没有可入库内容」，
  而不是入库一堆空块；
- 每块都带 `heading`（它所属的最近一级标题）：检索结果展示时用它告诉用户「这句来自
  哪一节」，这比只给文件名有用得多；
- 纯函数，不碰 IO，因此可以被穷举测试。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 单块的目标字符上限。取值依据：
#:
#: - 太小（<300）会把一个论点拆开，检索到的是半句话；
#: - 太大（>2000）会让一块混进多个主题，向量被平均掉，且两三块就吃掉上下文预算。
#:
#: 800 字符中文约 800 token 上下（宽字符按 1 token/字估算，见 `token_budget`），
#: 三块约 2.4k token——对常见模型是可接受的「引用几段原文」体积。
DEFAULT_CHUNK_CHARS = 800

#: 块间重叠字符数。约为上限的 12%：够覆盖一两个句子，又不会让重复内容占太多预算。
DEFAULT_OVERLAP_CHARS = 100

#: 小于这个字符数的块并进前一块。标题下一句话、列表最后一项这种碎块单独成块时，
#: 它自己几乎没有语义，检索命中它反而不如命中上下文更完整的邻块。
MIN_CHUNK_CHARS = 80

#: 段内切句用的标点。中英文都要：只用 `。` 会漏掉英文文档。
#:
#: **`.` 与 `;` 必须带后置条件**：裸 `.` 会在「3.14」「v1.2」「example.com」上切开，
#: 把一个数字或域名变成两句话（实测：`圆周率约等于 3.14` 被切成 `3.` / `14`）。
#: 所以英文句号只在「后面是空白或行尾」时才算边界——数字与域名里的点后面紧跟字符，
#: 自然被排除。中文的 `。！？；` 没有这个问题，直接切。
_SENTENCE_END = re.compile(r"(?<=[。！？；])\s*|(?<=[.!?;])(?=\s|$)")

#: Markdown 标题行。
_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


@dataclass(frozen=True)
class Chunk:
    """一个可入库的片段。

    `heading` 是**所属章节**，不是块自己的标题：它来自文档结构（最近的 Markdown
    标题），用于在检索结果里给用户一个位置线索。文档没有标题时为空串。
    """

    index: int
    text: str
    heading: str

    @property
    def chars(self) -> int:
        return len(self.text)


def split_sections(text: str) -> list[tuple[str, str]]:
    """按 Markdown 标题把文档切成 `(标题, 正文)` 段。

    标题行本身**保留在正文里**：它是这一节最强的主题信号（「## 退款政策」里的
    「退款」两字直接决定这块向量落在哪个语义区域），删掉反而让所有块长得一样。

    第一个标题之前的引言部分标题为空串。
    """
    sections: list[tuple[str, str]] = []
    current_heading = ""
    buffer: list[str] = []

    def flush() -> None:
        body = "\n".join(buffer).strip()
        if body:
            sections.append((current_heading, body))

    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            flush()
            buffer = []
            current_heading = match.group(2).strip()
            buffer.append(line)
            continue
        buffer.append(line)
    flush()
    return sections


def split_paragraphs(body: str) -> list[str]:
    """按空行切段；单段内的换行保留（代码块与列表依赖它）。"""
    paragraphs = re.split(r"\n\s*\n", body)
    return [paragraph.strip() for paragraph in paragraphs if paragraph.strip()]


def split_sentences(paragraph: str) -> list[str]:
    """按句末标点切句；切不动的（没有标点的超长串）原样返回一段。

    保留标点：`split` 用的是零宽后顾/前瞻，标点本身留在前一段末尾。丢掉标点会让
    相邻两块拼起来读着缺字。
    """
    parts = [piece.strip() for piece in _SENTENCE_END.split(paragraph)]
    return [piece for piece in parts if piece]


def pack_units(units: list[str], limit: int) -> list[str]:
    """把「句子/段落」这些最小单元贪心累积到 `limit` 以内。

    单个单元本身就超限时**独占一块**（不再硬切）：硬切会把一句话劈成两半，语义
    直接断掉；独占一块虽然超限，但至少是完整的，且调用方仍有 `max_chunk_chars`
    之外的硬上限（见 `truncate_to_budget`）。
    """
    packed: list[str] = []
    buffer = ""
    for unit in units:
        candidate = f"{buffer}\n{unit}" if buffer else unit
        if buffer and len(candidate) > limit:
            packed.append(buffer)
            buffer = unit
            continue
        buffer = candidate
    if buffer:
        packed.append(buffer)
    return packed


def _overlap_tail(text: str, overlap: int) -> str:
    """取尾部 `overlap` 个字符，尽量从句子边界开始。

    从句子边界开始是为了让重叠部分本身可读——「半句话」重复一次没有意义，还会
    让模型以为文档里真有两句一样的。
    """
    if overlap <= 0 or len(text) <= overlap:
        return ""
    tail = text[-overlap:]
    # 找 tail 里第一个句子边界，从它之后开始（这样重叠段以完整句子开头）
    for match in re.finditer(r"[。！？；.!?;]\s*", tail):
        candidate = tail[match.end() :].strip()
        if len(candidate) >= overlap // 3:
            return candidate
    return tail.strip()


def chunk_text(
    text: str,
    *,
    max_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
    min_chars: int = MIN_CHUNK_CHARS,
) -> list[Chunk]:
    """把一篇文档切成片段列表；空文档返回空列表。

    `max_chars <= 0` 时抛 `ValueError`：0 会让下面所有循环退化成「每句话一块」，
    是个静默的质量崩塌，不如直接拒绝。`overlap_chars` 会被夹到 `max_chars // 2`
    以内——重叠大于等于块长时，相邻块几乎完全重复，检索会把同一段内容占满名额。
    """
    if max_chars <= 0:
        raise ValueError("max_chars 必须大于 0：0 会让分块退化成每句一块")
    overlap = max(0, min(overlap_chars, max_chars // 2))

    if not text or not text.strip():
        return []

    # 打包上限要**扣掉重叠**：重叠是拼在块前面的额外内容，不扣的话每块实际长度会
    # 超出 `max_chars`，调用方按上限估的预算就全错。
    body_limit = max(max_chars - overlap, max_chars // 2)

    raw_blocks: list[tuple[str, str]] = []
    for heading, body in split_sections(text):
        for paragraph in split_paragraphs(body):
            # 段落本身超限时先切成句子，再按窗口累积；没超限时它就是最小单元。
            units = [paragraph] if len(paragraph) <= body_limit else split_sentences(paragraph)
            for piece in pack_units(units, body_limit):
                raw_blocks.append((heading, piece))

    # 合并过碎的块：把小于 min_chars 的块并进**前一块**，避免「一句话成块」。
    # 只有标题没有正文的那种块（`## 流程` 下一行就是下一个标题）同样并进去——
    # 它自己没有任何可检索的内容，单独成块只会浪费一个检索名额。
    merged: list[tuple[str, str]] = []
    for heading, piece in raw_blocks:
        if merged and (len(piece) < min_chars) and len(merged[-1][1]) + len(piece) + 1 <= body_limit:
            previous_heading, previous_text = merged[-1]
            merged[-1] = (previous_heading, f"{previous_text}\n{piece}")
            continue
        merged.append((heading, piece))

    # 首块如果是碎块（文档以一两行引言开头），没有「前一块」可并：并进**后**一块。
    if len(merged) > 1 and len(merged[0][1]) < min_chars and len(merged[0][1]) + len(merged[1][1]) + 1 <= body_limit:
        first_heading, first_text = merged.pop(0)
        second_heading, second_text = merged[0]
        merged[0] = (first_heading or second_heading, f"{first_text}\n{second_text}")

    # 加重叠：从第二块起，把上一块的尾部拼到自己前面。
    chunks: list[Chunk] = []
    previous_tail = ""
    for index, (heading, piece) in enumerate(merged):
        body = f"{previous_tail}\n{piece}".strip() if previous_tail else piece
        chunks.append(Chunk(index=index, text=body, heading=heading))
        previous_tail = _overlap_tail(piece, overlap)

    return chunks


def truncate_to_budget(text: str, max_chars: int) -> str:
    """硬上限兜底：单个单元本身就超限时截断（保留首尾）。

    与 `context_compaction.truncate_middle` 同一手法，但这里不 import 它——RAG 的
    入库路径不该依赖对话压缩模块（两个模块的演化理由完全不同，耦合会让其中一方
    的改动意外影响另一方）。
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = int(max_chars * 0.6)
    tail = max_chars - head - 20
    if tail <= 0:
        return text[:max_chars]
    return f"{text[:head]}\n…（已截断）…\n{text[-tail:]}"


__all__ = [
    "DEFAULT_CHUNK_CHARS",
    "DEFAULT_OVERLAP_CHARS",
    "MIN_CHUNK_CHARS",
    "Chunk",
    "chunk_text",
    "pack_units",
    "split_paragraphs",
    "split_sections",
    "split_sentences",
    "truncate_to_budget",
]
