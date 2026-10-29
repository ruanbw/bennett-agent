"""沙箱的客户端端点：心跳与状态；租约一律由服务端按 `(user_id, session_id)` 解析。

不给用户直接 exec 的接口：那会多一个必须与工具层同等严格、却只服务「自己敲命令」的授权面。
心跳写 Redis 而非数据库：每 30 秒刷一次时间戳的 UPDATE 是纯写放大，而键没了就等于心跳断了。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import User, get_async_session
from app.schemas.sandbox import (
    MAX_SESSION_ID_CHARS,
    SESSION_ID_PATTERN,
    SandboxArtifact,
    SandboxArtifactsResponse,
    SandboxHeartbeatRequest,
    SandboxStatusResponse,
)
from app.services.auth import current_active_user
from app.services.redis_client import get_redis
from app.services.sandbox.lease import get_active_lease, get_latest_lease, touch_heartbeat

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sandbox", tags=["sandbox"])

SESSION_ID_QUERY = Query(
    ...,
    max_length=MAX_SESSION_ID_CHARS,
    pattern=SESSION_ID_PATTERN,
    description="会话标识（sess_<...>）",
)


@router.post(
    "/heartbeat",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="沙箱心跳",
    description=(
        "会话页面可见且处于激活状态时每 30 秒调用一次；"
        "超过空闲阈值（默认 10 分钟）没有心跳且没有活动，机器会被自动回收。"
    ),
)
async def heartbeat(
    payload: SandboxHeartbeatRequest,
    user: User = Depends(current_active_user),  # noqa: B008
    session: AsyncSession = Depends(get_async_session),  # noqa: B008
) -> Response:
    """刷新这台机器的「客户端还在」标记，永远返回 204。

    有无机器必须无法区分（存在性探测），心跳失败也不该在 UI 上冒错误——它只是续命。
    """
    if not settings.sandbox_enabled:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    lease = await get_active_lease(session, user_id=user.id)
    if lease is not None:
        try:
            await touch_heartbeat(get_redis(), lease.id)
        except Exception:  # noqa: BLE001 - Redis 抖动不该让心跳 500；下一轮再试
            logger.warning("沙箱心跳写入失败 (lease=%s)", lease.id, exc_info=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/status",
    response_model=SandboxStatusResponse,
    summary="会话的沙箱状态",
    responses={404: {"description": "该会话还没有过执行环境"}},
)
async def sandbox_status(
    session_id: str = SESSION_ID_QUERY,
    user: User = Depends(current_active_user),  # noqa: B008
    session: AsyncSession = Depends(get_async_session),  # noqa: B008
) -> SandboxStatusResponse:
    """返回该用户最近一条租约（含已销毁的），让界面能说「已回收，产物在这里」。

    `session_id` 只校验形状、不参与查询：机器按用户分；404 只表示「从来没有过环境」。
    """
    if not settings.sandbox_enabled:
        raise HTTPException(status_code=404, detail="未启用执行环境")

    lease = await get_latest_lease(session, user_id=user.id)
    if lease is None:
        raise HTTPException(status_code=404, detail="该会话还没有执行环境")
    return SandboxStatusResponse(
        state=lease.state,
        expires_at=lease.hard_expires_at,
        artifact_uri=lease.artifact_uri,
    )


#: 产物在对象键里的文件名，与 `artifacts.py` 导出时写的两个名字对应；写死成白名单是为了
#: 不要 `s3:ListBucket`（导出只需要 `s3:PutObject`）。
_ARTIFACT_FILENAMES: tuple[str, ...] = ("workspace.tar.gz", "manifest.txt")


@router.get(
    "/artifacts",
    response_model=SandboxArtifactsResponse,
    summary="「我的电脑」的产物清单（含下载直链）",
    responses={404: {"description": "未启用执行环境"}},
)
async def sandbox_artifacts(
    session_id: str = SESSION_ID_QUERY,
    user: User = Depends(current_active_user),  # noqa: B008
    session: AsyncSession = Depends(get_async_session),  # noqa: B008
) -> SandboxArtifactsResponse:
    """把最近一条租约的 `artifact_uri` 翻成预签名直链。

    永远 200：「还没销毁过所以没产物」是最常见的情形，做成 404 会让每次轮询都变成一条错误日志。
    `session_id` 同样只回显不寻址；对象存储未配置时降级成空列表而不是 503（产物本就尽力而为）。
    """
    if not settings.sandbox_enabled:
        raise HTTPException(status_code=404, detail="未启用执行环境")

    lease = await get_latest_lease(session, user_id=user.id)
    if lease is None:
        return SandboxArtifactsResponse(session_id=session_id)

    artifacts: list[SandboxArtifact] = []
    if lease.artifact_uri:
        artifacts = await _describe_artifacts(lease.artifact_uri)

    return SandboxArtifactsResponse(
        session_id=session_id,
        lease_id=lease.id,
        state=lease.state,
        artifacts=artifacts,
    )


async def _describe_artifacts(artifact_uri: str) -> list[SandboxArtifact]:
    """`artifact_uri` 只是一个键（归档或清单），这里同目录按白名单探测，扩成 1~2 条清单项。

    两次导出（`sandbox_reset` 后重建）可能各留一个；探测比再存一张产物表便宜，也不用迁移。
    """
    from app.services.storage import ObjectStoreError, StorageNotConfigured, get_object_store

    try:
        store = get_object_store()
    except StorageNotConfigured:
        logger.info("沙箱产物：对象存储未配置，产物清单为空")
        return []

    prefix = artifact_uri.rsplit("/", 1)[0]
    expires_in = settings.s3_presign_expiry_seconds
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=expires_in)

    found: list[SandboxArtifact] = []
    for name in _ARTIFACT_FILENAMES:
        key = f"{prefix}/{name}"
        try:
            size = await store.stat_size(key)
            if size is None:
                continue
            url = await store.presigned_get_url(key, expires_in)
        except ObjectStoreError as exc:
            # 单个键探测失败不影响另一条：产物是尽力而为的能力，不该让清单整体 500
            logger.warning("沙箱产物：读取失败 (key=%s)：%s", key, exc)
            continue
        found.append(SandboxArtifact(name=name, size_bytes=size, download_url=url, expires_at=expires_at))

    return found
