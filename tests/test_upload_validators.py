"""上传文件的类型判定与对象键构造（纯函数，无 IO）。

这些规则出错的方式都是**安全类**问题，而不是「体验不好」：

- 只看扩展名：改名的可执行文件会被永久写进对象存储；
- 只看 MIME：纯文本没有魔数，任何真实文档都会被判成 `application/octet-stream`；
- 用户文件名参与对象键：`../` 与控制字符就变成了另一层注入面；
- 文件名原样写日志：换行可以伪造日志行。

所以下面每个用例都对着一条具体边界，而不是「跑通了」。
"""

from __future__ import annotations

import uuid

import pytest

from app.services.storage import build_storage_key, sanitize_display_name
from app.services.uploads.validators import (
    KIND_DOCX,
    KIND_HTML,
    KIND_PDF,
    KIND_TEXT,
    UploadValidationError,
    detect_kind,
)

# ---------------------------------------------------------------------
# 类型判定
# ---------------------------------------------------------------------


def test_pdf_requires_the_magic_header():
    assert detect_kind("a.pdf", b"%PDF-1.7\n...", "application/pdf").kind == KIND_PDF

    with pytest.raises(UploadValidationError, match="不像 PDF"):
        detect_kind("a.pdf", b"PK\x03\x04something", "application/pdf")


def test_docx_requires_the_zip_header():
    assert detect_kind("a.docx", b"PK\x03\x04...", "").kind == KIND_DOCX

    with pytest.raises(UploadValidationError, match="不像 Word"):
        detect_kind("a.docx", b"plain text", "")


def test_html_requires_a_leading_angle_bracket():
    assert detect_kind("a.html", b"  <!DOCTYPE html>", "text/html").kind == KIND_HTML

    with pytest.raises(UploadValidationError, match="不像 HTML"):
        detect_kind("a.html", b"just text", "text/html")


def test_gbk_text_is_accepted_and_binary_is_rejected():
    """GBK 中文的每个字节都 ≥0x80：不能因为「不是 UTF-8」就当成二进制。"""
    gbk = "退款政策：七天无理由".encode("gb18030")
    assert detect_kind("a.txt", gbk, "text/plain").kind == KIND_TEXT

    binary = bytes(range(0, 8)) * 20
    with pytest.raises(UploadValidationError, match="二进制"):
        detect_kind("a.txt", binary, "text/plain")


def test_unknown_extension_is_rejected_with_the_supported_list():
    with pytest.raises(UploadValidationError) as excinfo:
        detect_kind("a.exe", b"MZ", "application/octet-stream")

    assert ".exe" in str(excinfo.value)
    # 错误消息要能直接指导用户：支持哪些格式必须写出来
    assert "pdf" in str(excinfo.value) and "docx" in str(excinfo.value)


def test_declared_content_type_must_match_the_kind():
    """声明成图片的 .pdf 不是正常上传，直接拒绝。"""
    with pytest.raises(UploadValidationError, match="不一致"):
        detect_kind("a.pdf", b"%PDF-1.7", "image/png")


def test_empty_file_is_rejected():
    with pytest.raises(UploadValidationError, match="空"):
        detect_kind("a.txt", b"", "text/plain")


# ---------------------------------------------------------------------
# 文件名清洗
# ---------------------------------------------------------------------


def test_display_name_drops_path_components():
    """浏览器偶尔会带完整路径；把它当文件名展示会泄露本地目录结构。"""
    assert sanitize_display_name("../../etc/passwd") == "passwd"
    assert sanitize_display_name(r"C:\Users\me\Desktop\报告.pdf") == "报告.pdf"


def test_display_name_removes_control_characters():
    """换行/回车可以伪造日志行，必须清掉。"""
    cleaned = sanitize_display_name("a\nb\r\tc.txt")

    assert "\n" not in cleaned and "\r" not in cleaned and "\t" not in cleaned


def test_display_name_keeps_the_extension_when_truncating():
    long_name = "x" * 500 + ".pdf"

    cleaned = sanitize_display_name(long_name)

    assert len(cleaned) <= 200
    assert cleaned.endswith(".pdf")


def test_display_name_falls_back_when_nothing_is_left():
    assert sanitize_display_name("   ") == "unnamed"
    assert sanitize_display_name("...") == "unnamed"


# ---------------------------------------------------------------------
# 对象键
# ---------------------------------------------------------------------


def test_storage_key_never_contains_the_user_filename():
    """键只由服务端生成的部分组成：用户文件名一个字符都不进键。"""
    key = build_storage_key("uploads", uuid.UUID(int=1), extension="PDF")

    assert key.startswith("uploads/00000000-0000-0000-0000-000000000001/")
    # 扩展名归一化成小写，且来自白名单而不是原始文件名
    assert key.endswith(".pdf")
    assert key.count("/") == 2


def test_storage_key_is_unique_per_call():
    """同一用户上传同一文件两次会去重，但键本身仍必须不可猜、不重复。"""
    user_id = uuid.uuid4()
    assert build_storage_key("uploads", user_id) != build_storage_key("uploads", user_id)


def test_storage_key_rejects_unsafe_prefix():
    """前缀来自配置，但仍不能拼出含 `../` 的键。"""
    with pytest.raises(ValueError, match="不合法"):
        build_storage_key("../etc", uuid.uuid4())
