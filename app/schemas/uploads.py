"""上传接口的请求 / 响应模型。

## 为什么响应里带配额

前端要能回答「我还能传多少」。如果只返回文件列表，前端只能自己累加——而配额判定在
服务端（并发上传、失败记录也计入），两边的数字迟早对不上，用户会在「明明还有空间」
和「服务端说满了」之间来回撞。把 `used_bytes / max_bytes` 一起下发，前端不需要猜。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.db import UPLOAD_STATUSES


class UploadRead(BaseModel):
    """一条上传记录。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_name: str = Field(description="用户上传时的文件名（已清洗，仅用于展示）")
    content_type: str
    size_bytes: int
    status: str = Field(description="pending / processing / ready / failed")
    error: str | None = Field(default=None, description="失败原因（面向用户的一句话）")
    document_id: uuid.UUID | None = Field(default=None, description="解析成功后对应的知识库文档")
    attempts: int
    created_at: datetime

    @classmethod
    def status_values(cls) -> frozenset[str]:
        """合法状态集合（供文档与测试使用）。"""
        return UPLOAD_STATUSES


class UploadCreated(UploadRead):
    """上传结果。`duplicate=True` 表示命中已有记录，没有产生新对象。"""

    duplicate: bool = False


class UploadListResponse(BaseModel):
    """上传列表 + 配额。"""

    items: list[UploadRead]
    total: int
    used_bytes: int
    max_files: int
    max_bytes: int


class DownloadLink(BaseModel):
    """原件的临时下载链接。

    返回 URL 而不是直接 302：前端需要知道链接何时过期（`expires_in`），
    以便在用户点下载时提示「链接已过期，请重新打开」而不是撞一个 S3 的 XML 错误页。
    """

    url: str
    expires_in: int
