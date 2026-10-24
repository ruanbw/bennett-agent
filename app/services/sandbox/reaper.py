"""沙箱回收循环：把「没有心跳」的机器拆掉，并保证拆之前产物已经落了盘。

## 为什么它是独立循环，而不是每个请求顺手检查一次

回收的触发条件是**没人发请求了**——最后一次 `exec` 之后 10 分钟、或者硬上限 4 小时到点。
把这种「时间驱动、与请求无关」的工作挂到请求路径上，就会在「用户关掉页面」这个最常见
的场景里永远不触发。所以它跑在 worker 进程里（`main.py --worker`），与上传解析并列：
两者都是可重启、可水平扩、不占 web 事件循环的长任务。

## 判定顺序（错了会出两类事故）

1. **先看心跳**：Redis 里 `sbx:hb:<lease>` 还在 = 用户的会话页面还开着，跳过。
   不看心跳就按 `last_activity` 回收，会在「用户在读上一轮结果、还没提新问题」时
   把环境拆了——下一次执行又从零开始，用户看到的是「我刚才装的东西没了」。
2. **原子抢占**：`mark_destroying` 是条件 UPDATE。先读后写会让回收和一条长 `exec`
   撞车：机器被拆掉，而 `exec` 的输出永远回不来。
3. **先导出产物，再销毁机器**。顺序反了就没有产物可导出（`artifacts.py` 解释了为什么
   这件事不能省）。
4. **失败必须可重试**：`mark_destroy_failed` 不是终态，下一轮会重新抢占（`lease.py`
   里 `mark_destroying` 允许从 `destroy_failed` 重入）。静默吞掉失败 = 机器永久泄漏。

## 与租约层的分工

租约层只回答「数据库里哪些行看起来该回收了」；**心跳在 Redis 里，它不知道**。
所以这里做第二半判定，并把 `DestroyCandidate.reason` 一路带到 `mark_destroyed`——
归因（idle / hard_ttl / 会话被删 / 用户被封）必须来自判定那一刻，不能事后按时间再算。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db import async_session_maker
from app.services.redis_client import get_redis
from app.services.sandbox import get_provider
from app.services.sandbox.artifacts import export_workspace
from app.services.sandbox.base import SandboxError, SandboxProvider, owner_tag
from app.services.sandbox.lease import (
    DESTROY_ERROR,
    DESTROY_USER_BANNED,
    LeaseView,
    find_destroyable,
    find_stale_provisioning,
    heartbeat_alive,
    list_active_for_user,
    mark_destroy_failed,
    mark_destroyed,
    mark_destroying,
)

logger = logging.getLogger(__name__)

#: 回收循环的间隔。比心跳间隔（30s）大、比空闲阈值（600s）小得多：
#: 再小只是白扫表，再大则「静置 10 分钟回收」会变成「静置 15 分钟」。
REAP_INTERVAL_SECONDS = 60


@dataclass(slots=True)
class ReapSummary:
    """一轮回收的结果。写日志与测试断言共用，避免两边各数一遍。"""

    candidates: int = 0
    skipped_heartbeat: int = 0
    destroyed: int = 0
    failed: int = 0
    stale_provisioning: int = 0

    def __bool__(self) -> bool:
        return bool(self.destroyed or self.failed or self.stale_provisioning)


async def reap_once(*, now: datetime | None = None, provider: SandboxProvider | None = None) -> ReapSummary:
    """跑一轮回收。幂等、可重入；失败只影响这一台机器。"""
    summary = ReapSummary()
    moment = now or datetime.now(timezone.utc)

    async with async_session_maker() as session:
        candidates = await find_destroyable(session, now=moment, idle_seconds=settings.sandbox_idle_seconds)
        stale = await find_stale_provisioning(
            session, older_than=moment - timedelta(seconds=settings.sandbox_idle_seconds)
        )

    summary.candidates = len(candidates)

    redis = get_redis()
    for candidate in candidates:
        lease = candidate.lease
        try:
            if await heartbeat_alive(redis, lease.id):
                summary.skipped_heartbeat += 1
                continue
        except Exception:  # noqa: BLE001 - Redis 抖动时保守处理：这一轮先不动它
            logger.warning("回收：读取心跳失败，跳过 (lease=%s)", lease.id, exc_info=True)
            summary.skipped_heartbeat += 1
            continue

        reason = candidate.reason or DESTROY_ERROR
        async with async_session_maker() as session:
            if not await mark_destroying(session, lease.id):
                # 别人抢先了（另一个 worker，或者用户主动 reset）。不是错误。
                continue
        if await _destroy_lease(lease, reason=reason, provider=provider):
            summary.destroyed += 1
        else:
            summary.failed += 1

    # 卡在 provisioning 的残留：机器可能建出来了也可能没有。标记 + 告警，不猜着销毁。
    for lease in stale:
        async with async_session_maker() as session:
            await mark_destroy_failed(
                session,
                lease.id,
                error="建机过程未完成（控制面可能中途崩溃）；provider 侧是否留有机器需人工核对。",
            )
        summary.stale_provisioning += 1
        logger.warning("回收：租约卡在 provisioning，已标记失败待人工核对 (lease=%s)", lease.id)

    if summary:
        logger.info(
            "沙箱回收一轮：候选=%d 心跳存活=%d 已销毁=%d 失败=%d 残留=%d",
            summary.candidates,
            summary.skipped_heartbeat,
            summary.destroyed,
            summary.failed,
            summary.stale_provisioning,
        )
    return summary


async def run_reaper(stop: asyncio.Event) -> None:
    """worker 里的常驻循环。`stop` 由 SIGTERM 置位，等当前这一轮跑完再退。"""
    logger.info("沙箱回收循环启动（间隔 %ss）", REAP_INTERVAL_SECONDS)
    while not stop.is_set():
        try:
            await reap_once()
        except Exception:  # noqa: BLE001 - 一轮失败不该让循环退出，否则再也不回收
            logger.warning("沙箱回收失败（下一轮重试）", exc_info=True)
        try:
            # 用 wait_for 而不是 sleep：收到停机信号时能立刻退出，不用等满 60 秒。
            await asyncio.wait_for(stop.wait(), timeout=REAP_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue
    logger.info("沙箱回收循环已停止")


# ---------------------------------------------------------------------
# 强制销毁入口（会话被删 / 用户被封）
# ---------------------------------------------------------------------
async def destroy_leases_for_user(
    user_id: uuid.UUID,
    *,
    reason: str = DESTROY_USER_BANNED,
    provider: SandboxProvider | None = None,
) -> int:
    """封禁用户时拆掉他名下所有机器。

    封号是「立刻失效」语义（认证依赖会拒绝他），机器也必须一起失效——否则被改过的
    客户端仍能通过已存在的租约继续用算力。
    """
    async with async_session_maker() as session:
        leases = await list_active_for_user(session, user_id=user_id)
    return await _destroy_many(leases, reason=reason, provider=provider)


async def _destroy_many(
    leases: list[LeaseView], *, reason: str, provider: SandboxProvider | None = None
) -> int:
    destroyed = 0
    for lease in leases:
        async with async_session_maker() as session:
            if not await mark_destroying(session, lease.id):
                continue
        if await _destroy_lease(lease, reason=reason, provider=provider):
            destroyed += 1
    return destroyed


async def _destroy_lease(lease: LeaseView, *, reason: str, provider: SandboxProvider | None = None) -> bool:
    """导出产物 → 销毁机器 → 落终态。返回是否成功。

    任何一步失败都走 `mark_destroy_failed`：下一轮回收会重新抢它（失败不是终态）。
    """
    try:
        backend = provider or get_provider()
    except SandboxError as exc:
        async with async_session_maker() as session:
            await mark_destroy_failed(session, lease.id, error=f"provider 不可用：{exc}")
        return False

    artifact = await export_workspace(backend, lease, workdir=settings.sandbox_workdir)

    try:
        if lease.provider_ref:
            await backend.destroy(lease.provider_ref, owner_tag=owner_tag(lease.user_id))
    except SandboxError as exc:
        async with async_session_maker() as session:
            await mark_destroy_failed(session, lease.id, error=f"销毁失败：{exc}")
        logger.warning("回收：销毁失败 (lease=%s)：%s", lease.id, exc)
        return False

    async with async_session_maker() as session:
        await mark_destroyed(session, lease.id, reason=reason, artifact_uri=artifact)
    return True


__all__ = [
    "REAP_INTERVAL_SECONDS",
    "ReapSummary",
    "destroy_leases_for_user",
    "reap_once",
    "run_reaper",
]
