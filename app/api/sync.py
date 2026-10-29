"""跨设备同步的 HTTP 接口：两个端点，语义见 `app/services/sync_service.py`。

推幂等、拉用不透明游标（无变化时长轮询）；删除就是推一条 `deleted=true` 的行，少一个端点就少
一套权限与幂等语义。`400 invalid_cursor` 与 `410 resync_required` 对客户端动作相同：丢弃游标重拉。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.rate_limit import make_limiter, too_many
from app.config import settings
from app.db import User
from app.schemas.sync import PullResult, PushRequest, PushResult
from app.services import sync_service
from app.services.auth import current_active_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sync", tags=["sync"])

#: 同步限流桶。与对话桶分开：对话消耗上游额度，同步只消耗本机数据库。
_limiter: object | None = None


def get_sync_limiter():  # noqa: ANN201
    global _limiter
    _limiter = make_limiter(_limiter, max(settings.sync_rate_limit_per_minute, 1))
    return _limiter


async def enforce_sync_rate_limit(user: User = Depends(current_active_user)) -> None:  # noqa: B008
    """按账号计数的同步限流。

    声明了 `current_active_user` 依赖，因此只要用它认证就一定先生效——配额配成 0 也不会变成匿名可访问。
    """
    if settings.sync_rate_limit_per_minute <= 0:
        return
    allowed, retry_after = await get_sync_limiter().acheck(f"user:{user.id}")
    if not allowed:
        raise too_many(retry_after, limit=get_sync_limiter().limit)


@router.post(
    "/push",
    response_model=PushResult,
    summary="批量推送本机变更（与会话/消息的行级 upsert，幂等）",
    dependencies=[Depends(enforce_sync_rate_limit)],
)
async def push(request: PushRequest, user: User = Depends(current_active_user)) -> PushResult:  # noqa: B008
    """逐行接受或拒绝；整批不会因为某一行坏掉而失败。

    客户端拿到 `rejected` 后应把对应行标成错误并停止重试（`stale` 可以直接丢弃），
    其余行按已同步处理。
    """
    try:
        return await sync_service.push(user.id, request)
    except sync_service.TooManyRows as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{exc}（code=too_many_rows）",
        ) from exc


@router.get(
    "/pull",
    response_model=PullResult,
    summary="按不透明游标增量拉取（无变化时长轮询）",
    dependencies=[Depends(enforce_sync_rate_limit)],
)
async def pull(
    cursor: str | None = Query(default=None, description="上次响应里的 cursor；首次同步不传"),
    limit: int | None = Query(default=None, ge=1, description="每个数据流最多返回多少行"),
    timeout: float | None = Query(
        default=None,
        ge=0,
        le=120,
        description="长轮询挂起上限（秒）；不传用服务端默认值，0 表示立即返回",
    ),
    user: User = Depends(current_active_user),  # noqa: B008
) -> PullResult:
    try:
        since = sync_service.parse_cursor(cursor)
    except sync_service.InvalidCursor as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{exc}（code=invalid_cursor）",
        ) from exc

    try:
        page = await sync_service.pull(user.id, since, limit=limit)
        # 首次同步（不带游标）不挂起：客户端要的是全量；分页没拉完也不挂起。
        if cursor is None or page.has_more or page.conversations or page.messages:
            return page

        wait_seconds = (
            settings.sync_long_poll_seconds if timeout is None else min(timeout, settings.sync_long_poll_seconds)
        )
        if wait_seconds <= 0:
            return page

        await sync_service.wait_for_change(user.id, since, wait_seconds)
        return await sync_service.pull(user.id, since, limit=limit)
    except sync_service.StaleCursor as exc:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=f"{exc}（code=resync_required）",
        ) from exc
