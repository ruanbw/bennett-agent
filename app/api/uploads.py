"""文件上传接口：原件入对象存储 → 后台解析 → 写入知识库。

## 权限边界

所有端点在 `current_active_user` 之下，且每条查询都带 `user_id` 条件。上传文件是
**用户私有**的：管理员也看不到别人的文件（与知识库同一条原则）。

## 为什么上传和解析是分开的两步

上传接口只做「收下原件 + 建一条 pending 记录」，解析由独立 worker 执行：

- 一份 25MB 的 PDF 解析要几秒到几十秒，放在请求里会让浏览器一直转圈，而反代/网关
  通常 60 秒就断连——用户看到「上传失败」，实际文件已经收到了；
- worker 与 web 进程的生命周期完全不同：前者要能重启、扩缩、崩溃恢复，后者要快速
  响应。混在一起意味着「解析慢」会直接影响接口延迟。

代价是前端要轮询状态（`GET /api/uploads`），这比一个可能超时的长请求诚实得多。

## 错误码

- 400：文件本身不合法（类型、大小、内容与声明不符）——用户改文件即可；
- 404：上传功能未启用，或记录不存在 / 不属于当前用户（不区分这两种，避免探测）；
- 413：请求体超过全局上限（由中间件产生，早于本路由）；
- 503：对象存储不可用 / 未配置——运维问题，不是用户的错。
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import UploadedFile, User, get_async_session
from app.schemas.uploads import DownloadLink, UploadCreated, UploadListResponse, UploadRead
from app.services.auth import current_active_user
from app.services.storage import ObjectStoreError, StorageNotConfigured, get_object_store
from app.services.uploads.service import (
    UploadQuotaExceeded,
    create_upload,
    delete_upload,
    get_upload,
    list_uploads,
)
from app.services.uploads.validators import UploadValidationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/uploads", tags=["uploads"])


async def require_uploads_enabled() -> None:
    """关闭上传时返回 404，而不是 403。

    「这个部署没有这个功能」和「你没有权限」是两件事。对未启用的部署，返回 404 既不
    泄露功能存在，也不会让客户端去做一次无意义的重新登录。
    """
    if not settings.uploads_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


@router.post(
    "",
    response_model=UploadCreated,
    status_code=status.HTTP_201_CREATED,
    summary="上传文件（原件入对象存储，后台解析入库）",
    dependencies=[Depends(require_uploads_enabled)],
)
async def upload_file(
    file: UploadFile = File(..., description="要上传的文件（类型见 UPLOAD_ALLOWED_EXTENSIONS）"),  # noqa: B008
    user: User = Depends(current_active_user),  # noqa: B008
    session: AsyncSession = Depends(get_async_session),  # noqa: B008
) -> UploadCreated:
    """接收文件：校验 → 落对象存储 → 建 pending 记录。

    返回 201 表示**原件已安全落地**，不代表已经能检索——解析是异步的，状态见
    `status` 字段。`duplicate=true` 表示这份内容已经在库里（按 sha256 去重），
    没有占用第二份存储。
    """
    try:
        outcome = await create_upload(
            session,
            user_id=user.id,
            filename=file.filename or "",
            content_type=file.content_type,
            fileobj=file.file,
        )
    except UploadQuotaExceeded as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except UploadValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except StorageNotConfigured as exc:
        # 配置问题：给运维看的原因写日志，给用户一句不泄露部署细节的话
        logger.error("上传失败：对象存储未配置", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="文件存储暂不可用，请稍后重试或联系管理员。",
        ) from exc
    except ObjectStoreError as exc:
        logger.error("上传失败：对象存储错误", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="文件存储暂不可用，请稍后重试。",
        ) from exc

    payload = UploadRead.model_validate(outcome.record)
    return UploadCreated(**payload.model_dump(), duplicate=outcome.duplicate)


@router.get(
    "",
    response_model=UploadListResponse,
    summary="我的上传（含解析状态与配额）",
    dependencies=[Depends(require_uploads_enabled)],
)
async def list_my_uploads(
    user: User = Depends(current_active_user),  # noqa: B008
    session: AsyncSession = Depends(get_async_session),  # noqa: B008
) -> UploadListResponse:
    """列出上传记录（按时间倒序）与当前配额使用量。

    只返回最近 50 条：上传记录的作用是「看解析状态 / 删除原件」，不做归档浏览。
    数量上限本身是 200（`UPLOAD_MAX_FILES_PER_USER`），所以 50 条足够覆盖绝大部分场景，
    又不会让响应随用量线性增长。
    """
    records = await list_uploads(session, user.id, limit=50)
    count, used = (
        await session.execute(
            select(func.count(UploadedFile.id), func.coalesce(func.sum(UploadedFile.size_bytes), 0)).where(
                UploadedFile.user_id == user.id
            )
        )
    ).one()

    return UploadListResponse(
        items=[UploadRead.model_validate(record) for record in records],
        total=int(count),
        used_bytes=int(used),
        max_files=settings.upload_max_files_per_user,
        max_bytes=settings.upload_total_bytes_per_user,
    )


@router.get(
    "/{upload_id}/download",
    response_model=DownloadLink,
    summary="获取原件的临时下载链接",
    dependencies=[Depends(require_uploads_enabled)],
)
async def download_link(
    upload_id: uuid.UUID,
    user: User = Depends(current_active_user),  # noqa: B008
    session: AsyncSession = Depends(get_async_session),  # noqa: B008
) -> DownloadLink:
    """生成预签名 URL（默认 5 分钟）。

    不代理下载：为一个纯 IO 动作占用应用进程与带宽没有意义，而对象存储就是为这个
    场景设计的。链接有时效，转发出去的影响范围因此有界。
    """
    record = await get_upload(session, user.id, upload_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")

    expires_in = settings.s3_presign_expiry_seconds
    try:
        url = await get_object_store().presigned_get_url(record.storage_key, expires_in)
    except StorageNotConfigured as exc:
        logger.error("生成下载链接失败：对象存储未配置", exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="文件存储暂不可用。") from exc
    except ObjectStoreError as exc:
        logger.error("生成下载链接失败", exc_info=True)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="文件存储暂不可用。") from exc
    return DownloadLink(url=url, expires_in=expires_in)


@router.delete(
    "/{upload_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除上传记录、原件与派生的知识库文档",
    dependencies=[Depends(require_uploads_enabled)],
)
async def delete_my_upload(
    upload_id: uuid.UUID,
    user: User = Depends(current_active_user),  # noqa: B008
    session: AsyncSession = Depends(get_async_session),  # noqa: B008
) -> None:
    """删除一份上传（连带它的知识库文档与对象存储原件）。

    语义是「把这份资料彻底从我的账号里去掉」，因此派生文档也必须删——只删原件会留下
    「文件列表里没有了，但检索还能查到内容」的状态，用户会认为删除没生效。
    """
    removed = await delete_upload(session, user.id, upload_id)
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文件不存在")
