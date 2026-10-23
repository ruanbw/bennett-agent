"""从上传文件里抽取纯文本。

## 职责边界

这里只做「字节 → 文本」。切块、向量化、入库都在知识库层（`app/services/rag`），
因为那是两件独立的事：解析器只会因为「格式不支持 / 文件损坏」失败，而入库会因为
「没有配额 / embedding 不可用」失败。混在一起时错误信息会互相掩盖。

## 为什么这些解析器都在线程里跑

`pypdf` / `python-docx` 是纯同步 CPU + IO 混合的代码，一份 20MB 的 PDF 可能占用
几秒。放在事件循环里会阻塞同一进程的所有请求（包括正在流式输出的对话）。
调用方（worker）统一用 `asyncio.to_thread` 卸载，这在仓库里是既定模式。

## 编码回退顺序

文本类文件先试 UTF-8（含 BOM），再试 GB18030，最后 latin-1。顺序不能反：
GB18030 几乎能「解码」任意字节序列（不会抛错），先试它会把所有 UTF-8 中文变成乱码，
而且**看起来解析成功了**——这是最坏的一类失败。
"""

from __future__ import annotations

import io
import logging
import re

from app.services.uploads.validators import KIND_DOCX, KIND_HTML, KIND_PDF, KIND_TEXT

logger = logging.getLogger(__name__)

#: 连续空行压缩的上限：解析出来的 PDF 经常带七八个空行，白占切片预算。
_MAX_BLANK_LINES = 2


class DocumentParseError(RuntimeError):
    """文件无法解析（损坏、加密、扫描件无文字层、格式不支持）。

    与 `UploadValidationError` 的区别：那个是「不该收这个文件」，这个是「文件收对了，
    但处理不了」。两者的 HTTP 语义不同（400 vs 422/失败态），对用户的建议也不同。
    """


def extract_text(kind: str, data: bytes) -> str:
    """按类别抽取文本；失败统一抛 `DocumentParseError`（消息面向用户）。"""
    if not data:
        raise DocumentParseError("文件是空的。")

    if kind == KIND_PDF:
        text = _extract_pdf(data)
    elif kind == KIND_DOCX:
        text = _extract_docx(data)
    elif kind == KIND_HTML:
        text = _extract_html(data)
    elif kind == KIND_TEXT:
        text = _extract_plain(data)
    else:  # pragma: no cover - 校验层已经挡住了其他类别
        raise DocumentParseError(f"不支持的解析类别：{kind}")

    cleaned = normalize_text(text)
    if not cleaned.strip():
        raise DocumentParseError(
            "没能从文件里抽到任何文字。如果是扫描版 PDF，它只有图片没有文字层——需要先做 OCR 再上传。"
        )
    return cleaned


def normalize_text(text: str) -> str:
    """统一换行、去 NUL、压缩连续空行与行尾空白。

    这些看起来是洁癖，实际影响检索质量：PDF 抽出来的文本里满是 `\\x00`（会把
    PostgreSQL 的 TEXT 写入直接拒绝，报 `invalid byte sequence`）与七八个连续空行
    （白占切片预算，切出来的块里一半是空白）。
    """
    text = text.replace("\x00", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n" * _MAX_BLANK_LINES + "\n", text)
    return text.strip()


def _extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # 空密码能解开的加密 PDF 很常见（只是设了权限），先试空密码
            try:
                if reader.decrypt("") == 0:
                    raise DocumentParseError("PDF 已加密：请先去掉密码再上传。")
            except Exception as exc:  # noqa: BLE001 - pypdf 的异常类型不稳定
                raise DocumentParseError("PDF 已加密：请先去掉密码再上传。") from exc

        pages = [page.extract_text() or "" for page in reader.pages]
    except DocumentParseError:
        raise
    except (PdfReadError, ValueError, OSError) as exc:
        logger.warning("PDF 解析失败", exc_info=True)
        raise DocumentParseError("PDF 文件损坏或格式不完整，无法解析。") from exc

    return "\n\n".join(pages)


def _extract_docx(data: bytes) -> str:
    import docx

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - 损坏的 zip 会抛各种底层异常
        logger.warning("DOCX 解析失败", exc_info=True)
        raise DocumentParseError("Word 文档损坏或不是有效的 .docx 文件。") from exc

    parts: list[str] = [paragraph.text for paragraph in document.paragraphs]
    # 表格里的文字常常是文档的关键信息（对照表、参数表），但 python-docx 的
    # `paragraphs` 不包含它们；漏掉会让「文档明明写着」变成检索不到。
    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _extract_html(data: bytes) -> str:
    from bs4 import BeautifulSoup

    text = _decode_text(data)
    try:
        soup = BeautifulSoup(text, "html.parser")
    except Exception as exc:  # noqa: BLE001 - html.parser 很宽容，这里只是兜底
        logger.warning("HTML 解析失败", exc_info=True)
        raise DocumentParseError("HTML 文件无法解析。") from exc

    # 脚本与样式不是正文；留着只会污染检索（而且 JS 里常有 `refund` 这类误命中词）
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return soup.get_text("\n")


def _extract_plain(data: bytes) -> str:
    return _decode_text(data)


def _decode_text(data: bytes) -> str:
    """按 UTF-8 → GB18030 → latin-1 的顺序解码。

    `errors="replace"` 只用在最后一步：前两步失败说明编码不属于它们，而 latin-1
    永不失败。到这里仍保留替换字符（U+FFFD）是可接受的——比让整份文件上传失败好，
    而且用户能在正文里看到哪里出了问题。
    """
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")
