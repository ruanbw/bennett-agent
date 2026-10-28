"""沙箱的客户端端点：心跳与状态。

## 只有两个端点，且都不接受租约 id

- `POST /api/sandbox/heartbeat`：客户端告诉我们「这个会话还开着」；
- `GET /api/sandbox/status`：前端显示「正在开机 / 已回收 / 产物在哪」。

租约一律由服务端按 `(登录态 user_id, session_id)` 解析。客户端手里没有可伪造的租约句柄
——与工具层同一个决定。**不给用户直接 exec 的接口**：那会多一个必须与
工具层同等严格、却只服务「用户自己敲命令」的授权面，而本项目的 scope 里没有它。

## 心跳为什么写 Redis 而不是数据库

每个打开的会话每 30 秒一次写库，就是给自己制造写放大：一个 200 人在线的实例 =
每秒 6 次 UPDATE，而它唯一的作用是「刷新一个时间戳」。用 `SET key EX ttl`
写 Redis，语义完全一样（键没了 = 心跳断了），回收循环读一次 EXISTS 即可。
`last_activity`（服务端观察到的真实使用）仍然写库，因为它要参与审计与排查。
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
    """刷新这台机器的「客户端还在」标记。

    **永远返回 204**，即使没有机器：客户端不该需要知道「现在有没有环境」，
    而且「没有机器」和「别人的机器」必须无法区分（存在性探测）。
    心跳失败也不该在 UI 上冒错误——它只是续命，不是业务动作。
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
    """返回该用户**最近一条**租约（含已销毁的），让界面能说「已回收，产物在这里」。

    `session_id` 只用于校验形状（前端按会话发请求），**不参与查询**：机器是按用户分的，
    同一用户在任何会话里看到的都是同一台。

    404 只用于「这个用户从来没有过环境」：它与「环境已销毁」在界面上是两句不同的话，
    混成空对象会让前端把「还没开始」显示成「不见了」。
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


#: 产物在对象键里的文件名。与 `artifacts.py` 导出时写的两个名字一一对应。
#: 刻意写死成白名单而不是「按前缀列桶」：列桶要 `s3:ListBucket` 权限，而导出只需要
#: `s3:PutObject`。少一个权限面，也少一次分页遍历。
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

    ## 为什么不是 404

    `/status` 在「从来没有过环境」时返回 404，因为那是**该有东西却没有**。这里相反：
    「还没销毁过，所以没有产物」是最常见的情形，把它做成 404 会让前端的每次轮询都
    变成一条错误日志。所以永远 200，靠 `lease_id` / `state` / `artifacts` 三个字段
    表达三种不同的空。

    ## `session_id` 仍然不参与查询

    机器是按用户分的（`ensure_lease` 的「每用户一台」），同一用户在任何会话里看到的
    都是同一台机器、同一批产物。`session_id` 只回显，不寻址——与 `/status` 同一条
    设计，见 `app/tools/sandbox_scope.py` 的「身份不能来自模型参数 / 客户端标识」。

    ## 对象存储未配置时

    整段降级成空列表而不是 503：产物是「尽力而为」（导出失败也照常销毁机器，见
    `artifacts.py` 的失败策略）。一个没配 S3 的部署本来就没有产物，让它报错只会
    把一个已知的限制伪装成故障。
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
    """`artifact_uri` 是**一个**键（归档或清单），这里把它扩成 1~2 条清单项。

    导出逻辑只会写其中一个（体积超限退化成清单，见 `artifacts.py`），但两次导出
    （`sandbox_reset` 之后重建）可能各留下一个。同目录下按白名单探测，比在数据库里
    再存一张产物表便宜得多，也不需要迁移。
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
