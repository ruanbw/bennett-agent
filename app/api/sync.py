"""跨设备同步的 HTTP 接口：两个端点，语义见 `app/services/sync_service.py`。

## 为什么只有两个端点

- `POST /api/sync/push`：客户端把本机变更（会话 + 消息）批量推上来，幂等。
- `GET /api/sync/pull`：客户端带着不透明游标拉增量；没有变化时长轮询挂住。

删除不是独立端点：删会话就是推一条 `deleted=true` 的会话行。少一个端点就少一套
权限与幂等语义要对齐。

## 客户端必须按这两条错误动作自救

- `400 invalid_cursor`：游标不是本服务端能认的格式（客户端 bug 或协议升级）。
- `410 resync_required`：游标指向当前数据之外（服务端回滚 / 旧库恢复）。

两种情况下客户端的动作是同一个：**丢弃游标，从头做一次全量拉取**。分开报是为了让
日志能区分「客户端写错了」与「服务端状态变了」。

## 长轮询

`pull` 在「带游标 + 没有变化 + 没有分页剩余」时会挂起最多 `SYNC_LONG_POLL_SECONDS`
秒，有变更立刻唤醒（`asyncio.Event`，见 `notify_change`）。首次同步（不带游标）
永远不挂起——它要的是全量，不是「等新消息」。
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
    """按**账号**计数的同步限流。

    声明了 `current_active_user` 依赖，因此只要用它，认证就一定先生效——配额配成 0
    也不会变成匿名可访问。
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
