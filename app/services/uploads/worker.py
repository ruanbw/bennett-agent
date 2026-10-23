"""上传文件的解析 worker：Postgres 当队列，`FOR UPDATE SKIP LOCKED` 取任务。

## 为什么不用 Celery / RQ / arq

这个队列的规模是「每用户每分钟几个文件」，需求只有三条：任务不丢、多实例不重复处理、
崩溃后可恢复。这三条 Postgres 本身就能给：

- 任务就是 `uploaded_file` 表里的一行，**状态与业务数据在同一个事务里**——不会出现
  「队列里删了、库里还没写」的双写不一致；
- `FOR UPDATE SKIP LOCKED` 让多个 worker 并发取任务而不互相阻塞，这是 PG 的标准用法，
  不需要额外的锁服务；
- 租约（`processing_started_at`）让崩溃的 worker 手上的任务在超时后被重新领取。

引入 Celery 会带来 broker（又多一个 Redis 库/连接池）、序列化契约、以及「任务的输入
必须是可序列化的」这条额外约束——而我们的输入只是一个主键。用表当队列，运维上少的
是一整类「消息丢了但数据库有 / 数据库有但消息丢了」的排障。

## 重试策略

- `attempts` 在**领取时**自增，而不是失败时：worker 如果被 OOM Kill，失败回调根本不会
  执行，次数就永远不涨，一份有毒文件会被反复领取直到天荒地老。
- 可重试：对象存储抖动、未预期异常。不可重试：解析失败、校验失败、知识库拒绝
  （格式损坏、超限、未配置 embedding）——重试只会重复烧 CPU 与配额。
- 次数用尽后置 `failed` 并把原因写进记录。用户重新上传同一文件会把它重置为 pending
  （见 `service._reuse_or_requeue`），所以「修好配置再试一次」不需要删库。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import (
    UPLOAD_STATUS_FAILED,
    UPLOAD_STATUS_PENDING,
    UPLOAD_STATUS_PROCESSING,
    UPLOAD_STATUS_READY,
    UploadedFile,
    async_session_maker,
)
from app.services.rag import store as knowledge_store
from app.services.storage import StorageNotConfigured, get_object_store
from app.services.uploads import parsers
from app.services.uploads.validators import (
    EXTENSION_KINDS,
    extension_of,
)

logger = logging.getLogger(__name__)

#: 租约时长（秒）。超过它仍处于 processing 的行会被当作「worker 崩溃留下的」重新领取。
#:
#: 取值要大于「最慢的一次正常处理」：25MB 的 PDF + embedding 批量调用在最坏情况下
#: 可能接近 10 分钟。取得太小会让同一个文件被两个 worker 同时处理（重复烧配额），
#: 取得太大则崩溃后要等很久才恢复。15 分钟是这两者之间的保守选择。
PROCESSING_STALE_SECONDS = 900


async def claim_next(session: AsyncSession) -> UploadedFile | None:
    """领取一条待处理记录；没有则返回 None。

    可领取的条件：`pending`，或 `processing` 但租约已过期。后者是崩溃恢复的唯一机制——
    没有它，一次 OOM Kill 会让某些用户的文件永远停在「解析中」。
    """
    stale_before = datetime.now(timezone.utc) - timedelta(seconds=PROCESSING_STALE_SECONDS)
    stmt = (
        select(UploadedFile)
        .where(
            or_(
                UploadedFile.status == UPLOAD_STATUS_PENDING,
                and_(
                    UploadedFile.status == UPLOAD_STATUS_PROCESSING,
                    or_(
                        UploadedFile.processing_started_at.is_(None),
                        UploadedFile.processing_started_at < stale_before,
                    ),
                ),
            )
        )
        .order_by(UploadedFile.created_at)
        .limit(1)
        # SKIP LOCKED：多个 worker 同时取任务时，各自拿到不同的行而不是排队等锁
        .with_for_update(skip_locked=True)
    )
    record = await session.scalar(stmt)
    if record is None:
        return None

    record.status = UPLOAD_STATUS_PROCESSING
    record.processing_started_at = datetime.now(timezone.utc)
    # 领取即计数：见模块 docstring（OOM Kill 时失败回调不会执行）
    record.attempts += 1
    # 提交后锁释放，下一个 worker 可以取别的行
    await session.commit()
    await session.refresh(record)
    return record


async def process_record(record: UploadedFile) -> bool:
    """处理一条已领取的记录。返回是否处理成功。

    成功 = 原件已解析且文档已入库；失败时已按「可重试 / 不可重试」更新状态。
    """
    try:
        text = await _extract(record)
        await _ingest(record, text)
    except _NonRetryable as exc:
        await _mark_failure(record.id, str(exc), retry=False)
        logger.warning("上传解析失败（不可重试）: id=%s reason=%s", record.id, exc)
        return False
    except parsers.DocumentParseError as exc:
        # 文件损坏/加密/扫描件无文字层：重试读到的是同一份字节，只会重复烧 CPU
        await _mark_failure(record.id, str(exc), retry=False)
        logger.warning("上传解析失败（文件无法解析）: id=%s reason=%s", record.id, exc)
        return False
    except knowledge_store.KnowledgeError as exc:
        # 入库拒绝：未配置 embedding、单篇超限、文档数达上限。原因对用户有用，
        # 且重试不会自愈（用户删除旧文档或管理员配好模型后重传会重新排队）。
        await _mark_failure(record.id, str(exc), retry=False)
        logger.warning("上传入库失败（不可重试）: id=%s reason=%s", record.id, exc)
        return False
    except StorageNotConfigured:
        # 部署配置问题：重试不会自愈，但用户/运维修好后重新上传会把它重置为 pending
        await _mark_failure(record.id, "对象存储未配置，无法读取原件。", retry=False)
        logger.warning("上传解析失败：对象存储未配置", exc_info=True)
        return False
    except Exception:  # noqa: BLE001 - 可重试的未知故障（存储抖动、数据库瞬时错误）
        await _mark_failure(record.id, "处理失败，稍后会自动重试。", retry=True)
        logger.warning("上传解析失败（将重试）: id=%s", record.id, exc_info=True)
        return False

    logger.info("上传解析完成: id=%s chars=%d", record.id, len(text))
    return True


async def _extract(record: UploadedFile) -> str:
    """取原件并抽取文本；长度超限视为不可重试。"""
    data = await get_object_store().get_bytes(record.storage_key)
    kind = EXTENSION_KINDS.get(extension_of(record.original_name))
    if kind is None:
        raise _NonRetryable(f"不支持的文件类型：{record.original_name}")

    # 解析是阻塞 CPU（pypdf/docx 都是同步实现），必须离开事件循环，
    # 否则同一进程的 HTTP 请求（包括正在流式输出的对话）会被一起卡住。
    text = await asyncio.to_thread(parsers.extract_text, kind, data)

    limit = settings.upload_max_extracted_chars
    if len(text) > limit:
        raise _NonRetryable(f"抽取到的文本 {len(text):,} 字符超过单篇上限 {limit:,}：请拆分文件后分别上传。")
    return text


async def _ingest(record: UploadedFile, text: str):
    """把文本写入知识库，并在**同一个事务**里把记录标成 ready。

    同一事务是关键：文档写成功而状态更新失败，会留下「文档在库里但界面永远显示解析中」
    的记录；反过来则是「状态 ready 但检索不到内容」。两者都只能靠人工对账发现。
    """
    async with async_session_maker() as session:
        document = await knowledge_store.add_document(
            record.user_id,
            title=_document_title(record.original_name),
            text=text,
            source=f"upload:{record.original_name}",
            session=session,
        )
        row = await session.get(UploadedFile, record.id, with_for_update=True)
        if row is None:
            raise _NonRetryable("上传记录已被删除。")
        row.status = UPLOAD_STATUS_READY
        row.document_id = document.id
        row.error = None
        row.processing_started_at = None
        await session.commit()
        return document


async def _mark_failure(record_id: uuid.UUID, message: str, *, retry: bool) -> None:
    """写失败状态。`retry=True` 且还有次数时退回 pending。"""
    async with async_session_maker() as session:
        row = await session.get(UploadedFile, record_id, with_for_update=True)
        if row is None or row.status == UPLOAD_STATUS_READY:
            # 已经成功（或被删除）：失败回调不能再把它改回失败态
            return
        row.error = message
        row.processing_started_at = None
        if retry and row.attempts < settings.upload_max_attempts:
            row.status = UPLOAD_STATUS_PENDING
        else:
            row.status = UPLOAD_STATUS_FAILED
        await session.commit()


def _document_title(original_name: str) -> str:
    """文档标题：去掉扩展名的文件名（知识库列表里展示用）。"""
    stem, _, _ = (original_name or "").rpartition(".")
    return (stem or original_name or "未命名文档")[:200]


class _NonRetryable(RuntimeError):
    """不该重试的失败（格式、配额、配置）。"""


async def process_pending_once() -> bool:
    """取一条并处理；返回是否处理了任务（供测试与单次运行使用）。"""
    async with async_session_maker() as session:
        record = await claim_next(session)
    if record is None:
        return False
    await process_record(record)
    return True


async def run_worker(stop: asyncio.Event | None = None) -> None:
    """常驻循环：有任务就处理，空闲时按 `UPLOAD_WORKER_POLL_SECONDS` 轮询。

    单条任务失败**不会**退出循环：一份有毒文件不该让整个解析服务停摆。
    真正需要退出的是进程收到 SIGTERM/SIGINT（由 `main.py` 转成 `stop` 事件）。
    """
    stop = stop or asyncio.Event()
    logger.info(
        "上传解析 worker 启动: poll=%.1fs max_attempts=%d",
        settings.upload_worker_poll_seconds,
        settings.upload_max_attempts,
    )
    while not stop.is_set():
        try:
            handled = await process_pending_once()
        except Exception:  # noqa: BLE001 - 取任务本身失败（数据库抖动）也要能继续
            logger.warning("worker 取任务失败，稍后重试", exc_info=True)
            handled = False

        if handled:
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.upload_worker_poll_seconds)
        except (TimeoutError, asyncio.TimeoutError):
            continue
    logger.info("上传解析 worker 已停止")


__all__ = [
    "PROCESSING_STALE_SECONDS",
    "claim_next",
    "process_pending_once",
    "process_record",
    "run_worker",
]
