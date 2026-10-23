"""上传文件的类型判定与校验。

## 为什么同时看扩展名与内容

只看扩展名：把 `payload.exe` 改名成 `report.pdf` 就能过——后续解析器会拿到一堆二进制，
表现是「解析失败」，但在此之前它已经被永久写进了对象存储（占配额、可被下载）。
只看内容嗅探：不可靠。很多环境把一切非标准类型报成 `application/octet-stream`，
而纯文本 / Markdown 根本没有魔数，只能靠扩展名区分。

因此规则是**两者都查，任一不匹配就拒绝**，并且对每种格式给出可判定的特征：

    pdf   `%PDF-`（规范要求前 1024 字节内出现；这里取开头即可）
    docx  ZIP 头 `PK\\x03\\x04` + 扩展名 `docx`（docx 本质是 zip）
    html  `html/htm` 扩展名 + 开头是 `<`
    text  `md/txt` 扩展名 + 头部没有 NUL 字节（有 NUL 说明是二进制）

## 为什么不在这里做病毒扫描

扫描需要额外的扫描器与更新源，且必须在**写入存储之前**完成。本项目把它作为部署侧
的职责（对象存储的恶意文件扫描 / 上传网关），代码里留的是「类型与大小」这道边界。
在文档里写清楚这一点，比塞一个永远不更新的特征库更有用。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 嗅探时读取的头部字节数。512 足够覆盖这里所有格式的特征。
SNIFF_BYTES = 512

KIND_PDF = "pdf"
KIND_DOCX = "docx"
KIND_HTML = "html"
KIND_TEXT = "text"

#: 扩展名 → 解析器类别。白名单在 settings 里，这张表决定「允许的扩展名怎么解析」。
EXTENSION_KINDS: dict[str, str] = {
    "pdf": KIND_PDF,
    "docx": KIND_DOCX,
    "html": KIND_HTML,
    "htm": KIND_HTML,
    "md": KIND_TEXT,
    "txt": KIND_TEXT,
    # Markdown 的一种常见叫法；仍按纯文本解析（不渲染，只抽文本）
    "markdown": KIND_TEXT,
}

#: 每个类别允许的 MIME 声明。浏览器给的值不可信，但**声明与内容不一致**本身是信号：
#: 一个声明成 image/png 的 .pdf 不该被当成正常上传。
ALLOWED_CONTENT_TYPES: dict[str, set[str]] = {
    KIND_PDF: {"application/pdf", "application/octet-stream", ""},
    KIND_DOCX: {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
        "application/octet-stream",
        "",
    },
    KIND_HTML: {"text/html", "application/xhtml+xml", "text/plain", "application/octet-stream", ""},
    KIND_TEXT: {"text/plain", "text/markdown", "text/x-markdown", "application/octet-stream", ""},
}


class UploadValidationError(ValueError):
    """用户上传的文件不合法（类型、大小、内容与声明不符）。"""


@dataclass(frozen=True)
class DetectedUpload:
    kind: str
    extension: str


def extension_of(filename: str) -> str:
    """取小写扩展名（不含点）。没有扩展名时返回空串。"""
    _, _, suffix = (filename or "").rpartition(".")
    return suffix.strip().lower()


def detect_kind(filename: str, head: bytes, content_type: str | None) -> DetectedUpload:
    """判定文件类别；不合法时抛 `UploadValidationError`（消息直接面向用户）。

    `head` 只需要前 `SNIFF_BYTES` 字节——调用方在流式写入过程中顺手截取。
    """
    extension = extension_of(filename)
    kind = EXTENSION_KINDS.get(extension)
    if kind is None:
        raise UploadValidationError(
            f"不支持的文件类型 .{extension or '（无扩展名）'}。支持：{', '.join(sorted(EXTENSION_KINDS))}。"
        )

    declared = (content_type or "").split(";", 1)[0].strip().lower()
    if declared not in ALLOWED_CONTENT_TYPES[kind]:
        raise UploadValidationError(f"文件声明类型 {declared!r} 与扩展名 .{extension} 不一致。")

    if not head:
        raise UploadValidationError("文件是空的。")

    if kind == KIND_PDF:
        if not head.startswith(b"%PDF-"):
            raise UploadValidationError("扩展名是 .pdf，但内容不像 PDF（缺少 %PDF- 头）。")
    elif kind == KIND_DOCX:
        # docx 是 zip 容器；`PK\x03\x04` 也可能来自 xlsx/pptx，但扩展名已经限定 docx
        if not head.startswith(b"PK\x03\x04"):
            raise UploadValidationError("扩展名是 .docx，但内容不像 Word 文档（缺少 ZIP 头）。")
    elif kind == KIND_HTML:
        if not head.lstrip().startswith(b"<"):
            raise UploadValidationError("扩展名是 .html，但内容不像 HTML（没有以 < 开头）。")
    elif kind == KIND_TEXT:
        # NUL 字节在纯文本里几乎不可能出现；出现则说明是二进制被改了扩展名
        if b"\x00" in head:
            raise UploadValidationError("扩展名是文本格式，但内容包含二进制数据。")
        try:
            head.decode("utf-8")
        except UnicodeDecodeError:
            # 不是 utf-8 也可能是 GBK 中文文档——交给解析器按回退编码处理，
            # 这里只拒绝「明显不是文本」的内容（控制字节比例异常）。
            if _looks_binary(head):
                raise UploadValidationError("扩展名是文本格式，但内容不是可读文本。") from None

    return DetectedUpload(kind=kind, extension=extension)


def _looks_binary(head: bytes) -> bool:
    """粗略的二进制判定：**ASCII 控制字符**占比过高即视为二进制。

    为什么只数控制字符、不看可打印字符占比：GBK/GB18030 的中文每个字节都 ≥ 0x80，
    按「可打印 ASCII 占比」统计会把一篇正常的中文 txt 判成二进制（误杀比放过更糟，
    因为用户无法理解「我的文本文件为什么不是文本」）。二进制格式则相反：它们的头部
    几乎总是布满 0x00-0x1f，而文本里除了制表/换行/回车不该出现它们。

    阈值 5% 很宽松：一段合法文本里出现几个异常控制字符（例如粘贴进来的 ）
    不该被拦，而一个 ELF / PNG 的头部控制字符占比远高于这个值。
    """
    if not head:
        return False
    control = sum(1 for byte in head if byte < 32 and byte not in (9, 10, 13))
    return control / len(head) > 0.05
