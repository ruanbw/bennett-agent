"""沙箱租约的状态机、两个时钟（`last_activity` 在 DB、心跳在 Redis）与命令审计。
状态迁移只能是一条条件 UPDATE；心跳不进 DB，所以 `find_destroyable` 只给候选、全靠调用方复核。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from redis.asyncio import Redis
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import SandboxCommand, SandboxLease
from app.services.sandbox.base import (
    SandboxCapacityError,
    SandboxError,
    SandboxHandle,
    SandboxSpec,
    owner_tag,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# 状态。取值与 DB 里的字符串一致，别改：历史行不会跟着迁移。
# ---------------------------------------------------------------------
STATE_PROVISIONING = "provisioning"
STATE_READY = "ready"
STATE_PAUSED = "paused"
STATE_DESTROYING = "destroying"
STATE_DESTROYED = "destroyed"
STATE_FAILED = "failed"
STATE_DESTROY_FAILED = "destroy_failed"

# ---------------------------------------------------------------------
# 销毁原因（写进 `destroy_reason`）
# ---------------------------------------------------------------------
DESTROY_IDLE = "idle"
DESTROY_HARD_TTL = "hard_ttl"
DESTROY_SESSION_DELETED = "session_deleted"
DESTROY_USER_BANNED = "user_banned"
DESTROY_USER_RESET = "user_reset"
#: provider 侧销毁失败。独立原因，好让「机器还在、租约说没了」被一条 SQL 捞出来。
DESTROY_ERROR = "error"

#: 唯一索引覆盖的状态：同一 `(user_id, session_id)` 同时最多一条；`paused` 也算活着，
#: 它随时能恢复，同一会话再建一台会让用户看到两个 workspace。
LIVE_STATES = (STATE_PROVISIONING, STATE_READY, STATE_PAUSED)

#: 未结束的状态：用于展示与容量计数。`destroying` 也算——provider 确认之前那台机器的
#: CPU/内存还没还给节点池，按它不占容量放行会打爆节点池。
OPEN_STATES = (*LIVE_STATES, STATE_DESTROYING)

#: `destroying` 停留多久算卡住、该重试（见 `find_destroyable`）。没有它，被硬杀留下的
#: 一行会永久吃掉该用户唯一的容量位：`mark_destroying` 抢不回来、`get_active_lease` 也不认它。
DESTROYING_STALE_SECONDS = 15 * 60

#: 心跳键前缀。不带 `settings.redis_key_prefix`：它是跨部署共享的约定（运维脚本按它捞键），
#: 而 Redis 库本身已按环境分开。
HEARTBEAT_KEY_PREFIX = "sbx:hb:"


@dataclass(frozen=True, slots=True)
class LeaseView:
    """租约的只读快照。
    不把 ORM 行交给上层：改一个字段就会在下次 flush 时顺手写库，绕过条件 UPDATE。
    """

    id: str
    user_id: uuid.UUID
    session_id: str
    provider: str
    provider_ref: str | None
    state: str
    artifact_uri: str | None
    #: 只给界面显示「还有多久回收」；回收判定不读它（看 `DestroyCandidate.reason`）。
    hard_expires_at: datetime | None = None
    #: 已销毁时记的原因（idle / hard_ttl / …），界面据此说清「为什么没了」而不是「坏了」。
    destroy_reason: str | None = None


@dataclass(frozen=True, slots=True)
class DestroyCandidate:
    """一条回收候选：租约 + 判定时算出的销毁原因。
    原因必须随候选一起返回：`hard_expires_at` / `last_activity` 不在 `LeaseView` 里，重算会分裂 now。
    """

    lease: LeaseView
    reason: str


def new_lease_id() -> str:
    """`sbx_<32 hex>`：服务端生成的随机 id。
    不用自增：id 会进日志与告警，自增值会泄露「全站建过多少台」。
    """
    return f"sbx_{uuid.uuid4().hex}"


def _view(row: SandboxLease) -> LeaseView:
    return LeaseView(
        id=row.id,
        user_id=row.user_id,
        session_id=row.session_id,
        provider=row.provider,
        provider_ref=row.provider_ref,
        state=row.state,
        artifact_uri=row.artifact_uri,
        hard_expires_at=row.hard_expires_at,
        destroy_reason=row.destroy_reason,
    )


def _error_text(exc: BaseException) -> str:
    """失败的完整描述。带上类型名：`SandboxCapacityError` 与 `RuntimeError` 的处理方式完全不同。"""
    return f"{type(exc).__name__}: {exc}"[:2_000]


# ---------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------
async def get_active_lease(session: AsyncSession, *, user_id: uuid.UUID) -> LeaseView | None:
    """该用户当前可复用的机器（每人至多一台），只看 `LIVE_STATES`——`destroying` 的机器正在被拆。
    寻址只看 `user_id`：会话共享同一台，`session_id` 只记「最后谁在用」，拿它过滤就回到一会话一台。
    """
    row = await session.scalar(
        select(SandboxLease)
        .where(SandboxLease.user_id == user_id, SandboxLease.state.in_(LIVE_STATES))
        .order_by(SandboxLease.created_at.desc())
        .limit(1)
    )
    return None if row is None else _view(row)


async def get_latest_lease(session: AsyncSession, *, user_id: uuid.UUID) -> LeaseView | None:
    """该用户最近一条租约，不管状态。界面要能说清「环境已回收（产物在这里）」，而不是「没有环境」。"""
    row = await session.scalar(
        select(SandboxLease).where(SandboxLease.user_id == user_id).order_by(SandboxLease.created_at.desc()).limit(1)
    )
    return None if row is None else _view(row)


async def list_active_for_user(session: AsyncSession, *, user_id: uuid.UUID) -> list[LeaseView]:
    """某用户全部尚未结束的租约（容量面板 / 「我的环境」列表）。新的在前。"""
    rows = await session.scalars(
        select(SandboxLease)
        .where(SandboxLease.user_id == user_id, SandboxLease.state.in_(OPEN_STATES))
        .order_by(SandboxLease.created_at.desc())
    )
    return [_view(row) for row in rows]


async def find_destroyable(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    idle_seconds: int | None = None,
) -> list[DestroyCandidate]:
    """回收候选集：`ready`/`paused`/`destroy_failed`/`destroying` 中到期或空闲过久的租约。
    只给候选，不是「该杀的」：心跳在 Redis，调用方必须逐个用 `heartbeat_alive` 复核后再销毁。
    """
    now = now or datetime.now(timezone.utc)
    idle = settings.sandbox_idle_seconds if idle_seconds is None else idle_seconds
    idle_before = now - timedelta(seconds=idle)
    # `destroying` 的重试门槛要比「120s 打包 + 200MB 读回」宽松，又不能宽到硬杀后一直占着容量位。
    destroying_before = now - timedelta(seconds=DESTROYING_STALE_SECONDS)

    rows = await session.scalars(
        select(SandboxLease)
        .where(
            SandboxLease.state.in_((STATE_READY, STATE_PAUSED, STATE_DESTROY_FAILED, STATE_DESTROYING)),
            # `destroying` 的判据必须与另外三种互斥：通用的空闲分支对它几乎必然成立，
            # age 门槛会被直接绕过，所以两类分开写。
            or_(
                and_(
                    SandboxLease.state != STATE_DESTROYING,
                    or_(
                        SandboxLease.hard_expires_at < now,
                        # coalesce：`last_activity` 为空表示还没跑过命令，那就从创建起算空闲。
                        func.coalesce(SandboxLease.last_activity, SandboxLease.created_at) < idle_before,
                    ),
                ),
                and_(
                    SandboxLease.state == STATE_DESTROYING,
                    # 没有 `destroying_since` 的旧数据按 `created_at` 兜底：宁可晚点重试，
                    # 也别让它永久占着容量位。
                    func.coalesce(SandboxLease.destroying_since, SandboxLease.created_at) < destroying_before,
                ),
            ),
        )
        # 先到期的先回收：候选数受节点池容量约束，不发 limit 也不会失控。
        .order_by(SandboxLease.hard_expires_at)
    )
    return [
        DestroyCandidate(
            lease=_view(row),
            reason=(
                row.destroy_reason or DESTROY_ERROR
                if row.state == STATE_DESTROY_FAILED
                else (DESTROY_HARD_TTL if row.hard_expires_at < now else DESTROY_IDLE)
            ),
        )
        for row in rows
    ]


# ---------------------------------------------------------------------
# 创建
# ---------------------------------------------------------------------
async def find_stale_provisioning(session: AsyncSession, *, older_than: datetime) -> list[LeaseView]:
    """卡在 `provisioning` 的租约（控制面在建机中途被硬杀），特征是 `provider_ref` 为空。
    `older_than` 太短会误判正在启动的机器；机器是否存在只有 provider 知道，所以只标记 + 告警。
    """
    rows = (
        await session.scalars(
            select(SandboxLease)
            .where(SandboxLease.state == STATE_PROVISIONING, SandboxLease.created_at < older_than)
            .order_by(SandboxLease.created_at)
        )
    ).all()
    return [_view(row) for row in rows]


async def _reject_if_at_capacity(session: AsyncSession, user_id: uuid.UUID) -> None:
    """容量保护（不是付费分级）：超限直接抛错，不静默排队。
    不加全局锁，并发可能少量超发；在建机路径上加跨用户锁会把「节点池慢」升级成所有人排队。
    """
    per_user_limit = settings.sandbox_max_per_user
    mine = await session.scalar(
        select(func.count())
        .select_from(SandboxLease)
        .where(SandboxLease.user_id == user_id, SandboxLease.state.in_(OPEN_STATES))
    )
    if (mine or 0) >= per_user_limit:
        raise SandboxCapacityError(
            f"每个用户最多同时保留 {per_user_limit} 个执行环境：请先结束其他会话的环境，或在当前会话里继续用。"
        )

    global_limit = settings.sandbox_max_concurrent
    total = await session.scalar(
        select(func.count()).select_from(SandboxLease).where(SandboxLease.state.in_(OPEN_STATES))
    )
    if (total or 0) >= global_limit:
        raise SandboxCapacityError("执行环境资源已满，请稍后再试。")


def _build_spec(lease_id: str, user_id: uuid.UUID) -> SandboxSpec:
    """把配置 + 租约翻成 provider 契约。`owner_tag` 让机器自己记住归属。"""
    return SandboxSpec(
        lease_id=lease_id,
        template=settings.sandbox_template,
        template_version=settings.sandbox_template_version,
        owner_tag=owner_tag(user_id),
        cpu=settings.sandbox_cpu,
        memory_mb=settings.sandbox_memory_mb,
        workdir=settings.sandbox_workdir,
    )


async def ensure_lease(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    session_id: str,
    create: Callable[[SandboxSpec], Awaitable[SandboxHandle]],
    now: datetime | None = None,
) -> tuple[LeaseView, bool]:
    """取或建这个用户的机器，返回 `(租约, 是否本次新建)`；寻址只看 `user_id`，会话共享一台。
    顺序必须「插 `provisioning` → commit → `create` → 改 `ready`」，反过来并发会建出两台机器。
    """
    existing = await get_active_lease(session, user_id=user_id)
    if existing is not None:
        if existing.session_id != session_id:
            # 纯归因用途（排障时看这台机器最后给哪个会话用了），不参与寻址。
            await session.execute(
                update(SandboxLease).where(SandboxLease.id == existing.id).values(session_id=session_id)
            )
            await session.commit()
            existing = replace(existing, session_id=session_id)
        return existing, False

    await _reject_if_at_capacity(session, user_id)

    now = now or datetime.now(timezone.utc)
    lease_id = new_lease_id()
    row = SandboxLease(
        id=lease_id,
        user_id=user_id,
        session_id=session_id,
        provider=settings.sandbox_provider,
        provider_ref=None,
        template=settings.sandbox_template,
        template_version=settings.sandbox_template_version,
        state=STATE_PROVISIONING,
        created_at=now,
        last_activity=None,
        hard_expires_at=now + timedelta(seconds=settings.sandbox_hard_ttl_seconds),
    )
    try:
        # savepoint：撞唯一索引时只回滚这一条 INSERT，外层事务还能继续用（去读回赢家那行）。
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        # 另一路刚提交了同一用户的租约，这是并发下最正常的结果。
        winner = await get_active_lease(session, user_id=user_id)
        if winner is None:
            # 冲突了却读不到活动行：赢家刚终结了自己，重试才有意义，交给调用方。
            raise
        await session.commit()
        return winner, False
    await session.commit()

    try:
        handle = await create(_build_spec(lease_id, user_id))
    except BaseException as exc:
        # 连 `CancelledError` 一起拦：被取消的那台机器可能已经建出来了，停在
        # `provisioning` 的行会让这个会话以后每次都读回一台没有 ref 的租约。
        await _mark_provision_failed(session, lease_id, exc)
        raise

    claimed = await session.scalar(
        update(SandboxLease)
        .where(SandboxLease.id == lease_id, SandboxLease.state == STATE_PROVISIONING)
        .values(state=STATE_READY, provider_ref=handle.ref)
        .returning(SandboxLease.id)
    )
    await session.commit()
    if claimed is None:
        # 只可能发生在「有人在我们建机期间把这行从 provisioning 挪走」：不猜着自愈而是
        # 响亮地失败，因为这台机器的 ref 只在这里出现过，静默吞掉就永远查不到它。
        raise SandboxError(
            f"租约在建机期间离开了 provisioning（lease_id={lease_id}, ref={handle.ref}）：机器需要人工回收。"
        )

    return (
        LeaseView(
            id=lease_id,
            user_id=user_id,
            session_id=session_id,
            provider=settings.sandbox_provider,
            provider_ref=handle.ref,
            state=STATE_READY,
            artifact_uri=None,
        ),
        True,
    )


async def _mark_provision_failed(session: AsyncSession, lease_id: str, exc: BaseException) -> None:
    """把建机失败写成 `failed`，独立提交：调用方抛出后随时可能回滚。
    会 `rollback()` 掉会话里任何未提交的改动，所以调用方不该在同一会话上挂自己的写入。
    """
    # 丢掉失败前的任何未提交状态（`create` 不该写库，但 provider 实现里可能有别的写入）。
    await session.rollback()
    await session.execute(
        update(SandboxLease)
        .where(SandboxLease.id == lease_id, SandboxLease.state == STATE_PROVISIONING)
        .values(state=STATE_FAILED, error=_error_text(exc))
    )
    await session.commit()
    logger.warning("沙箱建机失败: lease_id=%s error=%s", lease_id, _error_text(exc))


# ---------------------------------------------------------------------
# 活动与状态迁移
# ---------------------------------------------------------------------
async def touch_activity(session: AsyncSession, lease_id: str, *, now: datetime | None = None) -> None:
    """记一次「服务端观察到的工作」（exec / 文件读写的开始与结束），只对 `LIVE_STATES` 生效。
    刻意不带 `user_id`：`lease_id` 必须由调用方先用 `get_active_lease(user_id=...)` 解析。
    """
    await session.execute(
        update(SandboxLease)
        .where(SandboxLease.id == lease_id, SandboxLease.state.in_(LIVE_STATES))
        .values(last_activity=now or datetime.now(timezone.utc))
    )
    await session.commit()


async def mark_destroying(session: AsyncSession, lease_id: str, *, now: datetime | None = None) -> bool:
    """抢占销毁权。返回是否抢到（`False` = 别人已在销毁，或它不是活动状态）。
    检查与改必须在同一条 UPDATE 里；`destroying` 只在超过 `DESTROYING_STALE_SECONDS` 后允许重抢。
    """
    moment = now or datetime.now(timezone.utc)
    stalled_before = moment - timedelta(seconds=DESTROYING_STALE_SECONDS)

    claimed = await session.scalar(
        update(SandboxLease)
        .where(
            SandboxLease.id == lease_id,
            or_(
                # 正常入口：三种「可以开始销毁」的状态
                SandboxLease.state.in_((STATE_READY, STATE_PAUSED, STATE_DESTROY_FAILED)),
                # 重入入口：`destroying` 只在那次销毁真的卡住后才允许重抢——无条件的
                # `state='destroying'` 会让两个并发回收者都满足条件各自提交。
                and_(
                    SandboxLease.state == STATE_DESTROYING,
                    func.coalesce(SandboxLease.destroying_since, SandboxLease.created_at) < stalled_before,
                ),
            ),
        )
        .values(state=STATE_DESTROYING, destroying_since=moment)
        .returning(SandboxLease.id)
    )
    await session.commit()
    return claimed is not None


async def mark_destroyed(
    session: AsyncSession,
    lease_id: str,
    *,
    reason: str,
    artifact_uri: str | None = None,
) -> None:
    """`destroying → destroyed`。只允许从 `destroying` 迁移。
    `reason` 由调用方判定时决定并原样传下来（重算会把归因写错）；`artifact_uri` 可为空。
    """
    await session.execute(
        update(SandboxLease)
        .where(SandboxLease.id == lease_id, SandboxLease.state == STATE_DESTROYING)
        .values(
            state=STATE_DESTROYED,
            destroyed_at=datetime.now(timezone.utc),
            # 清掉：留着会让「下次销毁」的 age 门槛拿上一次的时间算，永远判成立。
            destroying_since=None,
            destroy_reason=reason,
            artifact_uri=artifact_uri,
        )
    )
    await session.commit()


async def mark_destroy_failed(
    session: AsyncSession,
    lease_id: str,
    *,
    error: str,
    reason: str = DESTROY_ERROR,
) -> None:
    """`destroying | provisioning → destroy_failed`（provider 销毁失败、或建机过程卡死）。
    必须与重试路径成对存在：`mark_destroying` 允许重抢、`find_destroyable` 放进候选，少一条就永久泄漏。
    """
    await session.execute(
        update(SandboxLease)
        .where(
            SandboxLease.id == lease_id,
            SandboxLease.state.in_((STATE_DESTROYING, STATE_PROVISIONING)),
        )
        .values(
            state=STATE_DESTROY_FAILED,
            # 失败态不再用它做判据，留着会误导下一次抢占的时间判断。
            destroying_since=None,
            destroy_reason=reason,
            error=error[:2_000],
        )
    )
    await session.commit()


async def record_command(
    session: AsyncSession,
    lease_id: str,
    *,
    command: str,
    exit_code: int | None,
    duration_ms: int | None,
    output_bytes: int | None,
    output_head: str | None,
    truncated: bool,
) -> None:
    """写一条命令审计，不参与任何金额计算（计量只有 token）。
    `seq` 靠先锁租约行再取 `max(seq)+1` 保证并发唯一；`output_head` 原样落库，再截会扔掉尾部。
    """
    # 锁父行而不是命令表：命令表没有「每租约一行」的锚点，锁它等于锁住整个租约的审计。
    locked = await session.scalar(select(SandboxLease.id).where(SandboxLease.id == lease_id).with_for_update())
    if locked is None:
        # 租约行被级联删除了：命令已经跑完，让它失败会把成功的执行变成错误，所以只告警。
        logger.warning("租约不存在，跳过命令审计: lease_id=%s", lease_id)
        return

    next_seq = (
        await session.scalar(
            select(func.coalesce(func.max(SandboxCommand.seq), 0)).where(SandboxCommand.lease_id == lease_id)
        )
        or 0
    ) + 1
    session.add(
        SandboxCommand(
            lease_id=lease_id,
            seq=next_seq,
            command=command,
            exit_code=exit_code,
            duration_ms=duration_ms,
            output_bytes=output_bytes,
            output_head=output_head or "",
            truncated=truncated,
        )
    )
    await session.commit()


# ---------------------------------------------------------------------
# 心跳（Redis，不写库）
# ---------------------------------------------------------------------
def heartbeat_key(lease_id: str) -> str:
    """心跳键：`sbx:hb:<lease_id>`。运维脚本与面板按这个形状捞键。"""
    return f"{HEARTBEAT_KEY_PREFIX}{lease_id}"


async def touch_heartbeat(redis: Redis, lease_id: str, *, ttl_seconds: int | None = None) -> None:
    """刷新心跳 TTL（`SET ... EX`），默认 TTL 取 `sandbox_idle_seconds`：一次心跳 = 再给一个空闲窗口。
    `redis` 由调用方传入：心跳跑在独立循环里，连接生命周期与 HTTP 请求不同。
    """
    ttl = settings.sandbox_idle_seconds if ttl_seconds is None else ttl_seconds
    await redis.set(heartbeat_key(lease_id), "1", ex=ttl)


async def heartbeat_alive(redis: Redis, lease_id: str) -> bool:
    """心跳键存在 = 用户最近还在看这个会话。
    Redis 故障时让异常抛出去：返回 `False` 会让回收循环瞬间拆掉全部机器。
    """
    return bool(await redis.exists(heartbeat_key(lease_id)))


__all__ = [
    "DESTROY_ERROR",
    "DESTROY_HARD_TTL",
    "DESTROY_IDLE",
    "DESTROY_SESSION_DELETED",
    "DESTROY_USER_BANNED",
    "DESTROY_USER_RESET",
    "HEARTBEAT_KEY_PREFIX",
    "LIVE_STATES",
    "OPEN_STATES",
    "STATE_DESTROYED",
    "STATE_DESTROYING",
    "STATE_DESTROY_FAILED",
    "STATE_FAILED",
    "STATE_PAUSED",
    "STATE_PROVISIONING",
    "STATE_READY",
    "DestroyCandidate",
    "LeaseView",
    "ensure_lease",
    "find_destroyable",
    "find_stale_provisioning",
    "get_active_lease",
    "get_latest_lease",
    "heartbeat_alive",
    "heartbeat_key",
    "list_active_for_user",
    "mark_destroy_failed",
    "mark_destroyed",
    "mark_destroying",
    "new_lease_id",
    "record_command",
    "touch_activity",
    "touch_heartbeat",
]
