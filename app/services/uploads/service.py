"""上传编排：校验 → 落临时文件 → 存对象 → 建待解析记录。

## 为什么要先落临时文件，而不是把请求体读进内存

三件事都需要同一份字节流：算 sha256、判断大小、嗅探类型；而且之后还要原样写进
对象存储。全部读进内存的写法在 25MB × 并发数下会直接把进程打爆（Python 的 bytes 还
有额外开销）。这里流式写入临时文件、边写边算哈希与长度，内存占用与文件大小无关。

临时文件用 `delete=False` + 显式 `finally` 清理：`NamedTemporaryFile` 的自动删除在
Windows 上会因为「文件仍被打开」而失败，而显式清理在两种平台上行为一致。

## 去重与幂等

`(user_id, sha256)` 唯一。重复上传同一份文件时**不再占第二份存储、不再烧第二次
embedding 配额**，直接把已有记录返回给用户（前端据此提示「这份文件已在库里」）。
如果已有记录处于 failed，则重置为 pending 重新解析——否则「重传一次」永远修不好
一个曾经失败的文件。

并发上传同一文件的竞态由唯一约束兜住：捕获 IntegrityError 后回查一次，返回那条记录。

## 配额

两个维度都查：文件数（防止用一堆小文件刷满行数）与总字节（防止用几个大文件占满盘）。
两者都是**每用户**的，且失败记录也计入——否则「传失败 → 删除 → 重传」会变成一个
无限写入通道。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
import uuid
from dataclasses import dataclass
from typing import BinaryIO

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import UPLOAD_STATUS_FAILED, UPLOAD_STATUS_PENDING, UploadedFile
from app.services.rag import store as knowledge_store
from app.services.storage import (
    StorageNotConfigured,
    build_storage_key,
    get_object_store,
    sanitize_display_name,
)
from app.services.uploads.validators import (
    SNIFF_BYTES,
    UploadValidationError,
    detect_kind,
)

logger = logging.getLogger(__name__)

#: 流式读取的块大小。1MB 是磁盘与内存之间的常见折中：再小会让系统调用成为瓶颈，
#: 再大则单次分配的峰值内存明显上升（并发 20 个上传时就是 20MB 的差）。
CHUNK_SIZE = 1024 * 1024


class UploadTooLarge(UploadValidationError):
    """文件超过 `UPLOAD_MAX_BYTES`。"""


class UploadQuotaExceeded(UploadValidationError):
    """文件数或总字节超出该用户的配额。"""


@dataclass(frozen=True)
class UploadOutcome:
    """上传结果。`duplicate=True` 表示命中了已有记录（没有产生新对象）。"""

    record: UploadedFile
    duplicate: bool


async def create_upload(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    filename: str,
    content_type: str | None,
    fileobj: BinaryIO,
) -> UploadOutcome:
    """接收一个上传文件，返回落库后的记录。

    抛 `UploadValidationError`（400 类，消息可直接给用户）或
    `StorageNotConfigured`（部署配置问题）。
    """
    display_name = sanitize_display_name(filename)
    if not settings.is_upload_extension_allowed(display_name):
        raise UploadValidationError(f"不支持的文件类型：{display_name}。支持：{settings.upload_allowed_extensions}。")

    tmp_path: str | None = None
    try:
        spooled = await asyncio.to_thread(_spool_to_temp, fileobj)
        tmp_path = spooled.path
        size, digest, head = spooled.size, spooled.digest, spooled.head
        detected = detect_kind(display_name, head, content_type)

        existing = await _find_existing(session, user_id, digest)
        if existing is not None:
            return await _reuse_or_requeue(session, existing)

        await _enforce_quota(session, user_id, size)

        key = build_storage_key(settings.s3_prefix, user_id, extension=detected.extension)
        object_store = get_object_store()
        await object_store.ensure_bucket()
        await object_store.put_file(key, tmp_path, _effective_content_type(content_type, detected.kind))

        record = UploadedFile(
            id=uuid.uuid4(),
            user_id=user_id,
            original_name=display_name,
            content_type=_effective_content_type(content_type, detected.kind),
            size_bytes=size,
            sha256=digest,
            storage_key=key,
            status=UPLOAD_STATUS_PENDING,
            attempts=0,
        )
        session.add(record)
        try:
            await session.commit()
        except IntegrityError:
            # 并发上传同一文件：另一条请求先插入了。回滚后返回已有记录。
            await session.rollback()
            existing = await _find_existing(session, user_id, digest)
            if existing is None:
                raise
            return UploadOutcome(record=existing, duplicate=True)

        await session.refresh(record)
        logger.info(
            "上传已入库: id=%s user=%s size=%d kind=%s key=%s",
            record.id,
            user_id,
            size,
            detected.kind,
            key,
        )
        return UploadOutcome(record=record, duplicate=False)
    finally:
        if tmp_path:
            _unlink_quietly(tmp_path)


async def list_uploads(session: AsyncSession, user_id: uuid.UUID, *, limit: int = 50) -> list[UploadedFile]:
    rows = await session.scalars(
        select(UploadedFile)
        .where(UploadedFile.user_id == user_id)
        .order_by(UploadedFile.created_at.desc())
        .limit(limit)
    )
    return list(rows)


async def get_upload(session: AsyncSession, user_id: uuid.UUID, upload_id: uuid.UUID) -> UploadedFile | None:
    """按 `(id, user_id)` 取记录。

    双条件是硬性的：只按 id 查会让拿到别人 id 的人看到文件名、状态与下载链接，
    与知识库的越权读是同一条边界（见 `app/api/knowledge.py` 的权限说明）。
    """
    return await session.scalar(
        select(UploadedFile).where(UploadedFile.id == upload_id, UploadedFile.user_id == user_id)
    )


async def delete_upload(session: AsyncSession, user_id: uuid.UUID, upload_id: uuid.UUID) -> bool:
    """删除上传记录、对应的知识库文档与对象存储里的原件。

    执行顺序是「库 → 对象」：库里删成功而对象删失败，最坏情况是一个孤儿对象占空间
    （可被运维清理脚本回收）；反过来会留下「记录指向一个不存在的对象」的死链，
    用户每次点下载都 500。
    """
    record = await get_upload(session, user_id, upload_id)
    if record is None:
        return False

    document_id = record.document_id
    storage_key = record.storage_key
    await session.delete(record)
    await session.commit()

    if document_id is not None:
        try:
            await knowledge_store.delete_document(user_id, document_id)
        except Exception:  # noqa: BLE001 - 文档删除失败不该让上传记录残留
            logger.warning("删除派生知识库文档失败: document=%s", document_id, exc_info=True)

    try:
        await get_object_store().delete(storage_key)
    except Exception:  # noqa: BLE001 - 对象删除是尽力而为，见 docstring
        logger.warning("删除对象存储原件失败（将成为孤儿对象）: key=%s", storage_key, exc_info=True)
    return True


async def _find_existing(session: AsyncSession, user_id: uuid.UUID, digest: str) -> UploadedFile | None:
    return await session.scalar(
        select(UploadedFile).where(UploadedFile.user_id == user_id, UploadedFile.sha256 == digest)
    )


async def _reuse_or_requeue(session: AsyncSession, existing: UploadedFile) -> UploadOutcome:
    """重复上传：已完成/进行中的直接返回；失败过的重置为待解析。"""
    if existing.status != UPLOAD_STATUS_FAILED:
        return UploadOutcome(record=existing, duplicate=True)

    existing.status = UPLOAD_STATUS_PENDING
    existing.attempts = 0
    existing.error = None
    existing.processing_started_at = None
    await session.commit()
    await session.refresh(existing)
    logger.info("重复上传命中失败记录，已重新排队: id=%s", existing.id)
    return UploadOutcome(record=existing, duplicate=True)


async def _enforce_quota(session: AsyncSession, user_id: uuid.UUID, incoming_size: int) -> None:
    count, total = (
        await session.execute(
            select(func.count(UploadedFile.id), func.coalesce(func.sum(UploadedFile.size_bytes), 0)).where(
                UploadedFile.user_id == user_id
            )
        )
    ).one()

    if int(count) >= settings.upload_max_files_per_user:
        raise UploadQuotaExceeded(
            f"上传文件数已达上限（{settings.upload_max_files_per_user} 个）：请先删除不再需要的文件。"
        )
    if int(total) + incoming_size > settings.upload_total_bytes_per_user:
        limit_mb = settings.upload_total_bytes_per_user / (1024 * 1024)
        raise UploadQuotaExceeded(f"上传总容量已达上限（{limit_mb:.0f} MB）：请先删除不再需要的文件。")


def _effective_content_type(content_type: str | None, kind: str) -> str:
    """落库/落对象存储的 MIME。

    `application/octet-stream` 与空值都会被归一化成该类别最常见的类型：存储里保留一个
    信息量为零的类型，会让「下载后在浏览器里打开」变成「下载一个未知文件」。
    """
    declared = (content_type or "").split(";", 1)[0].strip().lower()
    if declared and declared != "application/octet-stream":
        return declared
    return {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "html": "text/html",
        "text": "text/plain",
    }.get(kind, "application/octet-stream")


#: 流式落盘的结果。把路径放进返回值、而不是塞进模块级变量：后者在「写入过程中抛
#: 异常」时会让路径丢失（临时文件永久残留），也会在并发上传时互相覆盖。
@dataclass(frozen=True)
class _SpooledUpload:
    path: str
    size: int
    digest: str
    head: bytes


def _spool_to_temp(fileobj: BinaryIO) -> _SpooledUpload:
    """把上传流写入临时文件，返回路径、字节数、sha256 与头部字节。

    三条约束都在同一次遍历里完成：写入、哈希、大小上限。**超限时立刻中止并删除**，
    而不是先收完再判断——「先收完」等于允许任何人用超大 body 把磁盘写满。

    失败时自己删文件：异常路径上调用方拿不到路径，不能指望它清理。
    """
    digest = hashlib.sha256()
    size = 0
    head = b""
    limit = settings.upload_max_bytes

    path = ""
    try:
        with tempfile.NamedTemporaryFile(prefix="bennett-upload-", suffix=".bin", delete=False) as handle:
            path = handle.name
            while True:
                chunk = fileobj.read(CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise UploadTooLarge(
                        f"文件超过上限 {limit / (1024 * 1024):.0f} MB。"
                        "请拆分后再上传，或联系管理员调整 UPLOAD_MAX_BYTES。"
                    )
                if len(head) < SNIFF_BYTES:
                    head += chunk[: SNIFF_BYTES - len(head)]
                digest.update(chunk)
                handle.write(chunk)
    except BaseException:
        _unlink_quietly(path)
        raise

    if size == 0:
        _unlink_quietly(path)
        raise UploadValidationError("文件是空的。")
    return _SpooledUpload(path=path, size=size, digest=digest.hexdigest(), head=head)


def _unlink_quietly(path: str) -> None:
    """删除临时文件；失败只记 debug 日志。

    临时目录本来就会被系统回收，而这里的失败（文件被占用、权限）不应该盖掉真正的
    错误——调用方可能正在向上抛「文件太大」。
    """
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    except OSError:
        logger.debug("清理临时文件失败: %s", path, exc_info=True)


def reset_temp_state_for_tests() -> None:
    """保留给测试的钩子：临时文件现在随返回值清理，无模块级状态可重置。"""
    return None


__all__ = [
    "UploadOutcome",
    "UploadQuotaExceeded",
    "UploadTooLarge",
    "StorageNotConfigured",
    "create_upload",
    "delete_upload",
    "get_upload",
    "list_uploads",
    "reset_temp_state_for_tests",
]
