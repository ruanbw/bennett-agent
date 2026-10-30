"""沙箱回收循环：把「没有心跳」的机器拆掉，并保证拆之前产物已经落了盘。

必须常驻：触发条件是「没人发请求了」，挂在请求路径上就永远不触发。判定顺序为心跳 →
条件 UPDATE 抢占 → 导出产物 → 销毁；销毁失败不是终态，下一轮重来，否则机器永久泄漏。
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
    DESTROY_HARD_TTL,
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
            # 硬上限到期的候选不看心跳：provider 侧的 `timeout=hard_ttl` 已经把机器杀了，心跳只
            # 证明「客户端还在看」。用心跳否决它，租约就会停在 `ready` 且 `provider_ref` 指向一台
            # 不存在的机器——之后每次 exec 都 `SandboxNotFound`，工具回「请重试」却永远重建不出新
            # 机器，用户只能等心跳过期。软时钟只能给软上限续命。
            if candidate.reason != DESTROY_HARD_TTL and await heartbeat_alive(redis, lease.id):
                summary.skipped_heartbeat += 1
                continue
        except Exception:  # noqa: BLE001 - Redis 抖动时保守处理：这一轮先不动它
            logger.warning("回收：读取心跳失败，跳过 (lease=%s)", lease.id, exc_info=True)
            summary.skipped_heartbeat += 1
            continue

        reason = candidate.reason or DESTROY_ERROR
        try:
            async with async_session_maker() as session:
                if not await mark_destroying(session, lease.id):
                    # 别人抢先了（另一个 worker，或者用户主动 reset）。不是错误。
                    continue
            if await _destroy_lease(lease, reason=reason, provider=provider):
                summary.destroyed += 1
            else:
                summary.failed += 1
        except Exception:  # noqa: BLE001 - 一台失败只影响这一台
            # 不隔开就会穿出这个 for、被 `run_reaper` 的最外层接住：整轮中止，后面所有到期机器
            # 都要再等一轮（60s），而这一台会停在 `destroying`，要等 `DESTROYING_STALE_SECONDS`
            # （15 分钟）才能重抢——期间它按 `OPEN_STATES` 占着该用户唯一的容量位。
            logger.warning("回收：处理租约时出现未预期异常，跳过这一台 (lease=%s)", lease.id, exc_info=True)
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

    封号是「立刻失效」语义（认证依赖会拒绝他）；机器也必须一起失效，否则被改过的客户端
    仍能靠已存在的租约继续用算力。
    """
    async with async_session_maker() as session:
        leases = await list_active_for_user(session, user_id=user_id)
    return await _destroy_many(leases, reason=reason, provider=provider)


async def _destroy_many(leases: list[LeaseView], *, reason: str, provider: SandboxProvider | None = None) -> int:
    destroyed = 0
    for lease in leases:
        try:
            async with async_session_maker() as session:
                if not await mark_destroying(session, lease.id):
                    continue
            if await _destroy_lease(lease, reason=reason, provider=provider):
                destroyed += 1
        except Exception:  # noqa: BLE001 - 一台失败不能放过剩下的机器
            # 封禁/删号的语义是「立刻全部失效」：这里抛出去会让剩下几台机器继续跑，
            # 而调用方（封禁端点）拿到的是 500，运维只能从日志里猜还有哪几台没拆。
            logger.warning("强制销毁：处理租约时出现未预期异常，跳过这一台 (lease=%s)", lease.id, exc_info=True)
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

    artifact = await _export_or_none(backend, lease)

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


async def _export_or_none(backend: SandboxProvider, lease: LeaseView) -> str | None:
    """导出产物，失败一律当「没有产物」。
    导出链路里的 `provider.exec` 抛的不是 `SandboxError`（httpx 超时、SDK 内部异常），不在这里
    拦就整台机器都不会被销毁——产物是尽力而为，机器必须回收（否则对象存储抖一下就等于永久占机）。
    `CancelledError` 是 `BaseException`，停机取消照样穿出去，不会被当成导出失败。
    """
    try:
        return await export_workspace(backend, lease, workdir=settings.sandbox_workdir)
    except Exception:  # noqa: BLE001 - 见 docstring
        logger.warning("回收：导出产物时出现未预期异常，继续销毁 (lease=%s)", lease.id, exc_info=True)
        return None


__all__ = [
    "REAP_INTERVAL_SECONDS",
    "ReapSummary",
    "destroy_leases_for_user",
    "reap_once",
    "run_reaper",
]
