"""沙箱租约的状态机、两个时钟与命令审计。

## 为什么这张表是「唯一真相」

会话历史仍然由客户端回放，但机器是服务端独有的长生命周期资源：上面有什么文件、
跑了什么进程，客户端不可能知道也不可能重建。所以租约落在 Postgres 里而不是内存里——
多 worker 部署下，内存态必然对不上，而「两个 worker 各自认为机器还活着」的代价是一次
重复计费与一次把别人的机器拆掉。

## 所有状态迁移都是条件 UPDATE

`UPDATE ... WHERE id=:id AND state='ready' RETURNING id`，rowcount=0 说明有人抢先了。
**绝不能先 SELECT 再 UPDATE**：回收循环和一条正在跑的长任务会同时在场，先读后写会让
回收把机器从长任务下面抽走。这个文件里凡是改状态的地方都只有
一条语句，就是为了让这件事在代码形状上无法退化。

## 两个时钟，一个在库里一个在 Redis

- `last_activity`（本文件，DB）：服务端观察到的工作（exec / 文件读写）。它证明**机器还在被用**；
- 心跳（本文件末尾三个函数，Redis）：客户端页面可见时每 30s 刷一次 TTL。它证明**用户还在看**。

心跳不写 Postgres：每用户每 30s 一次写库是自伤。代价是回收判定必须
跨两个存储，所以 `find_destroyable` 只给**候选集**，最终判定留给调用方结合
`heartbeat_alive` 完成——见该函数的 docstring。

## `sandbox_command` 不参与计费

本项目的计量只有 token。这张表存在的理由是「不再逐条命令打断用户」之后，
审计与排障还能回答「它当时到底跑了什么」。
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
#: provider 侧销毁失败。保留为独立原因，是为了让「机器还在、租约说没了」这类
#: 对账问题能被一条 SQL 捞出来。
DESTROY_ERROR = "error"

#: 唯一索引覆盖的状态：同一 `(user_id, session_id)` 同时最多一条。
#:
#: `paused` 也算「活着」——它随时能恢复，同一会话再建一台会让用户看到两个 workspace。
LIVE_STATES = (STATE_PROVISIONING, STATE_READY, STATE_PAUSED)

#: 尚未结束的状态：既用于展示（用户可能同时看到用着的与正在回收的），也用于容量计数。
#: `destroying` 两边都算：在 provider 确认之前，那台机器的 CPU/内存并没有还给节点池，
#: 按它不占容量去放行会让节点池被打爆。
OPEN_STATES = (*LIVE_STATES, STATE_DESTROYING)

#: `destroying` 停留多久算「卡住了」、该被回收循环重试。见 `find_destroyable` 的 docstring。
#:
#: 这个值必须是**有**的。没有它的时候，一行 `destroying` 会永久吃掉该用户唯一的容量位
#: （`_reject_if_at_capacity` 把 `destroying` 算进 `OPEN_STATES`），而同时
#: `mark_destroying` 不允许从 `destroying` 重入、`get_active_lease` 也不认它——用户看到
#: 的是「执行环境资源已满」，除了人工改库没有出路。触发窗口很宽：在
#: 「抢占已提交」与「终态已提交」之间进程被 SIGKILL/OOM/滚动发布，中间包含 120 秒的
#: 打包与最多 200MB 的读回。
DESTROYING_STALE_SECONDS = 15 * 60

#: 心跳键前缀。**不带** `settings.redis_key_prefix`：
#: 它是跨部署共享的约定（运维脚本、面板都按这个前缀捞），而 Redis 库本身已经按环境分开了。
HEARTBEAT_KEY_PREFIX = "sbx:hb:"


@dataclass(frozen=True, slots=True)
class LeaseView:
    """租约的只读快照。

    刻意不把 ORM 行交给上层：泄漏一个绑在会话上的对象出去，调用方改一个字段就会在
    下一次 flush 时**顺手写库**，绕过条件 UPDATE。这里只给「查到了什么」。
    """

    id: str
    user_id: uuid.UUID
    session_id: str
    provider: str
    provider_ref: str | None
    state: str
    artifact_uri: str | None
    #: 绝对上限到点时间。只给界面显示「还有多久回收」用，回收判定不读它
    #: （判定看的是 `DestroyCandidate.reason`，两者不能混）。
    hard_expires_at: datetime | None = None
    #: 已销毁时记的原因（idle / hard_ttl / session_deleted / user_banned / user_reset）。
    #: 界面据此说清「为什么没了」——只显示「已回收」会让用户以为是故障。
    destroy_reason: str | None = None


@dataclass(frozen=True, slots=True)
class DestroyCandidate:
    """一条回收候选：租约 + **判定时**算出的销毁原因。

    原因必须与候选一起返回，而不是让回收循环自己按时间再算一遍：`hard_expires_at`
    与 `last_activity` 不在 `LeaseView` 里（那是给工具层的视图），而重新算一次就要再来
    一次查询，还会出现「判定用 now、写库用另一个 now」的分裂。调用方只需原样传给
    `mark_destroyed(reason=...)`。
    """

    lease: LeaseView
    reason: str


def new_lease_id() -> str:
    """`sbx_<32 hex>`：服务端生成的随机 id。

    随机而不是自增：id 会出现在日志与告警里，自增值会泄露「全站一共建过多少台」，
    而顺序性对任何调用方都没有用（租约靠 `(user_id, session_id)` 解析，不靠 id 排序）。
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
    """取该用户当前**可复用**的机器（每个用户至多一台）。

    只覆盖唯一索引盯着的三个状态（`LIVE_STATES`）：`destroying` 的机器已经半只脚进坟墓，
    复用它等于把命令发给一台正在被拆的机器。

    **不再按会话区分**：同一个用户在不同会话里用的是同一台机器（需求：不要为每个会话
    各建一台）。`session_id` 只保留「最后是谁在用」这条归因信息，不参与寻址——
    拿它当过滤条件就等于又回到「一会话一台」。
    """
    row = await session.scalar(
        select(SandboxLease)
        .where(SandboxLease.user_id == user_id, SandboxLease.state.in_(LIVE_STATES))
        .order_by(SandboxLease.created_at.desc())
        .limit(1)
    )
    return None if row is None else _view(row)


async def get_latest_lease(session: AsyncSession, *, user_id: uuid.UUID) -> LeaseView | None:
    """该用户**最近一条**租约，不管它是什么状态。

    与 `get_active_lease` 的区别是它会把 `destroyed/failed` 也返回：界面要能说清
    「环境已被回收（产物在这里）」，而不是只能显示「没有环境」——后者会让用户以为
    自己从来没建过，或者以为我们弄丢了他的东西。
    """
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
    """回收候选集：`ready` / `paused` 且「空闲超时」或「绝对上限到点」。

    ## 这里只返回**候选**，不是「该杀的」

    心跳在 Redis（`heartbeat_alive`），本函数只看数据库。少了 Redis 那一半判定，
    「用户关掉页面但 agent 还在跑长任务」与「机器挂着没人用」在库里长得一模一样。
    调用方必须逐个复核：

        for candidate in candidates:
            if await heartbeat_alive(redis, candidate.lease.id):
                continue
            ...

    反过来，`last_activity` 在库里就是为了让「没有心跳但还在干活」的情况不被误杀。

    ## 归因顺序

    `hard_expires_at` 先判：两个时钟同时到点时，`hard_ttl` 是更准确的原因（它说明
    「这台机器活太久了」，而 idle 只是它的后果），对账时也更好解释。

    `paused` 也要能回收：暂停的机器照样占着磁盘与快照，而且用户可能再也不回来。

    `destroy_failed` 同样进候选：**失败不是终态**。它不在这里的话，一台销毁失败的机器
    会同时不被回收循环重试、也不被当成活动租约，是最典型的永久泄漏。这类候选的 reason
    沿用当初记下的 `destroy_reason`（那才是「为什么决定拆它」的真实答案），没有则记
    `DESTROY_ERROR`——不能按现在的时间重新算一遍，那会把归因写错。

    `destroying` 也进候选，但它有**独立的 age 门槛**（`DESTROYING_STALE_SECONDS`）：
    回收循环在正常销毁期间也会让行停在这个状态，太早重试会和正在干活的那个回收者
    撞车（虽然有 `mark_destroying` 的条件 UPDATE 兜底，白跑一趟没意义）。超过门槛就
    说明那次销毁**中断了**，必须有人接手——否则该用户的容量位永久被占。
    """
    now = now or datetime.now(timezone.utc)
    idle = settings.sandbox_idle_seconds if idle_seconds is None else idle_seconds
    idle_before = now - timedelta(seconds=idle)
    # `destroying` 的重试门槛：抢到 `destroying` 之后，导出产物（最多 120s 打包 +
    # 200MB 读回）加销毁的往返本来就慢，门槛必须比它宽松；但也不能太宽，否则进程被
    # 硬杀后那台机器的容量位要一直占着。
    destroying_before = now - timedelta(seconds=DESTROYING_STALE_SECONDS)

    rows = await session.scalars(
        select(SandboxLease)
        .where(
            SandboxLease.state.in_((STATE_READY, STATE_PAUSED, STATE_DESTROY_FAILED, STATE_DESTROYING)),
            # `destroying` 的判据必须与另外三种**互斥**，不能用 `or_` 里再加一条：
            # 回收前它本来就是空闲的，通用的空闲分支对它几乎必然成立，age 门槛会被
            # 直接绕过。所以两类分开写。
            or_(
                and_(
                    SandboxLease.state != STATE_DESTROYING,
                    or_(
                        SandboxLease.hard_expires_at < now,
                        # coalesce：`last_activity` 为空表示还没跑过任何命令，那台机器从创建起就在计空闲。
                        func.coalesce(SandboxLease.last_activity, SandboxLease.created_at) < idle_before,
                    ),
                ),
                and_(
                    SandboxLease.state == STATE_DESTROYING,
                    # 没有 `destroying_since` 的行是旧数据（加列之前抢下的）：按 created_at
                    # 兜底，宁可晚一点重试也不要让它永久占着容量位。
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
    """找「卡在 provisioning」的租约（控制面在建机过程中被硬杀）。

    这类行的特征是没有 `provider_ref`：机器可能建出来了也可能没有，只有 provider 侧
    知道。所以这里**不猜**着销毁（按空 ref 去销毁只会失败），而是交给回收循环标记
    `failed` 并告警——留一条「有一台机器下落不明」的记录，比悄悄删掉行安全。

    `older_than` 由调用方给（一般是 now - idle_ttl）：建机本身要几秒到几十秒，
    阈值太短会把正在正常启动的机器判成残留。
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
    """容量保护：先看该用户，再看全局。

    这是**容量保护而不是付费分级**：超限直接抛错、不静默排队，因为排队的
    表现是「点了运行之后界面转了一分钟」，比明确说「已满」更难解释。

    并发下可能少量超发（两路同时通过检查）。这里刻意不加全局锁：代价是多一台机器，
    而在建机路径上加一把跨用户的锁，会把「节点池慢」升级成「所有人排队」。真正的
    硬上限应该在节点池调度层（P2），不在控制面。
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
    """取或建**这个用户的**机器，返回 `(租约, 是否本次新建)`。

    ## 每用户一台

    同一个用户在不同会话里共享同一台机器：切换会话不该让刚才 `pip install` 的东西消失，
    也不该让一个人同时占着两三台。所以寻址只看 `user_id`，`session_id` 只记录
    「最后是谁在用」（`sandbox_lease.session_id` 会随复用被改写）。

    代价（写在这里避免以后当成 bug）：同一用户的两个会话共享 `/home/sandbox/workspace`，
    一个会话里写的文件在另一个会话里也看得见。这是「一台机器」的必然结果，不是泄漏——
    它们属于同一个人。

    ## 为什么先落库再建机器

    幂等点是 `user_id` 上的**部分唯一索引**，不是应用层的判断。所以顺序
    必须是「插入一行 `provisioning` → 调 `create` → 改 `ready`」：

    - 反过来（先建机器再插行）在并发下会建出两台，而唯一索引只能挡住第二条行，
      挡不住已经烧掉的两台机器的钱；
    - 插入后立刻提交，是为了让并发的第二路（agent 一轮里同时调两个工具是真实存在的）
      能读到这一行，而不是在唯一索引上等我们完成一次可能几十秒的冷启动。
      事务也不能横跨 `create()`：那会把一条连接和一把行锁按住整个冷启动时间。

    **提交在这里，而不是交给调用方**：跨 `create()` 的事务本身就是上面说的反模式，
    而调用方（工具层）拿到的是请求级会话，它没有「该在哪儿提交」的信息。

    ## 失败

    `create` 抛任何异常都会把该行标成 `failed` 并把异常**原样**抛出：工具层要按异常
    类型决定文案（`SandboxCapacityError` → 「资源已满」，其余 → 「环境没起来」）。
    留一行 `failed` 而不是删掉，是为了保留「哪台机器没起来、为什么」的线索——删了就只剩
    一条 user_id，排查时什么都看不到。

    ## 两个已知边界（都是刻意不在这里处理）

    - 并发下第二路可能读到一台还在 `provisioning`、`provider_ref` 仍为空的租约。等它
      比再建一台便宜，所以这里直接返回；拿空 ref 去 `exec` 会得到 `SandboxNotFound`，
      调用方应把 `provisioning` 当作「马上就好」而不是「环境坏了」。
    - 进程在建机中途被硬杀（OOM/`SIGKILL`）会留一行永远 `provisioning` 的租约，而该用户
      的后续调用会一直读回它。清理这类残留由回收循环负责（`find_stale_provisioning`）。
    """
    existing = await get_active_lease(session, user_id=user_id)
    if existing is not None:
        if existing.session_id != session_id:
            # 复用别人的会话留下的机器时，把「最后使用者」改成当前会话。
            # 纯归因用途（排障时看「这台机器最后给哪个会话用了」），不参与寻址。
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
        # 另一路刚刚提交了同一会话的租约。这不是错误，是并发下最正常的结果。
        winner = await get_active_lease(session, user_id=user_id)
        if winner is None:
            # 唯一索引确实冲突了，却读不到活动行：说明赢家在这次读之前又终结了自己。
            # 这种情况重试才有意义，交给调用方（而不是在这里假装成功一台不存在的机器）。
            raise
        await session.commit()
        return winner, False
    await session.commit()

    try:
        handle = await create(_build_spec(lease_id, user_id))
    except BaseException as exc:
        # 连 `CancelledError` 一起拦：请求被取消的那台机器可能**已经建出来了**，而停在
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
        # 只可能发生在「有人在我们建机期间把这行从 provisioning 挪走」——当前没有任何
        # 代码路径会这么做，所以不猜着自愈（比如悄悄销毁），而是响亮地失败：
        # 这台机器的 ref 只在这里出现过，静默吞掉就永远查不到它。
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
    """把建机失败写成 `failed`。失败记录必须独立提交：调用方抛出后随时可能回滚。

    这里会 `rollback()` 掉会话里任何未提交的改动。`ensure_lease` 的契约是「自己管事务」
    （建机要跨一次网络往返，事务不能横跨它），所以调用方不该在同一个会话上挂着自己的
    写入等它——那样哪一边先提交都会变成别人的意外。
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
    """记一次「服务端观察到的工作」（exec / 文件读写的开始与结束）。

    刻意不带 `user_id`：`lease_id` 必须是调用方**先用** `get_active_lease(user_id=...,
    session_id=...)` 解析出来的。构造性授权就落在「工具参数里没有 id」这一步，
    这是那之后的第二步——把用户输入直接喂给这个函数才是缺陷。

    只对 `LIVE_STATES` 生效：给一台 `destroying` 的机器续命会让回收循环与使用方互相拉扯。
    """
    await session.execute(
        update(SandboxLease)
        .where(SandboxLease.id == lease_id, SandboxLease.state.in_(LIVE_STATES))
        .values(last_activity=now or datetime.now(timezone.utc))
    )
    await session.commit()


async def mark_destroying(session: AsyncSession, lease_id: str, *, now: datetime | None = None) -> bool:
    """抢占销毁权。返回是否抢到（`False` = 别人已经在销毁，或它本来就不是活动状态）。

    整条语义就靠这一条语句：条件是 **原子**的一部分，所以「检查」与「改」之间不存在
    窗口——这正是回收循环不会把机器从一条长 `exec` 下面抽走的原因，也是
    「并发下只有一个调用者能抢到」的唯一保证。

    `destroy_failed` 与 `destroying` 都在允许的源状态里：销毁失败（provider 报错、导出
    超时）的机器仍然在跑、仍然占着内存，不重试就等于永久泄漏。`destroying` 同理——它
    可能是**一次被中断的**销毁留下的（进程在「抢占已提交」与「终态已提交」之间被
    SIGKILL/OOM/滚动发布杀掉，中间还夹着 120 秒打包与最多 200MB 读回）。不许它重入，
    那行就永久占着该用户唯一的容量位：`_reject_if_at_capacity` 把 `destroying` 算进
    `OPEN_STATES`，而 `get_active_lease` 又不认它 —— 用户看到的是「资源已满」，
    除了人工改库没有出路。

    **重入必须带 age 条件**（`DESTROYING_STALE_SECONDS`）：无条件允许
    `state='destroying'` 看起来更简单，但那样两个并发回收者会**同时**抢到——它们各自
    都满足条件、各自提交，而「抢占唯一」是这条语句全部的意义。所以门槛落在 UPDATE 的
    `WHERE` 里（与 `find_destroyable` 的候选判据同一个常量），而不是靠调用方自律。

    在这里提交：抢到之后调用方要去做产物导出 + provider destroy（几秒到几十秒），
    期间让整条连接和一个未提交的状态变更挂着，会让别的回收者以为这台机器还能用。
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
                # 重入入口：`destroying` 只在那次销毁**真的卡住了**之后才允许重抢。
                # 无条件的 `state='destroying'` 会让两个并发回收者同时抢到（都满足条件
                # 的 UPDATE 各自提交），而「抢占必须唯一」正是这条语句存在的理由。
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

    `reason` 由调用方在判定时决定（见 `DestroyCandidate.reason`）并一路传下来：
    到这里再按时间算一遍，会得到一个和「为什么决定销毁」不一致的归因。

    `artifact_uri` 可以为空：产物导出失败不该让销毁卡住——机器已经没了，留着一个
    `destroying` 的行只会让它永远占着容量。失败原因由调用方写进告警。
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
    """`destroying | provisioning → destroy_failed`。provider 销毁失败、或建机过程卡死时走这里。

    **不允许静默**：失败必须落成一个能被查询到的状态，而不是让那行留在
    `destroying` 里——停在 `destroying` 的租约既不会被回收循环重试，也不会被当成活动租约，
    是最容易永久泄漏机器的一种中间态。

    `reason` 记的是「当初为什么要销毁」（idle / hard_ttl / …），`error` 记的是「销毁
    本身为什么失败」。两者都留着：重试成功时 `mark_destroyed` 会重写 reason，而重试之前，
    这对信息是唯一能解释这台机器处境的记录。

    从 `provisioning` 也能进来，是为了处理「控制面在建机中途被硬杀」的残留：那种行没有
    `provider_ref`，机器是否存在只有 provider 侧知道，所以只标记 + 告警、不猜着销毁。

    重试路径：`mark_destroying` 允许从 `destroy_failed` 重新抢占，`find_destroyable` 也把
    `destroy_failed` 放进候选集——这两条必须成对存在，只有其中一条就等于「失败即永久泄漏」。
    """
    await session.execute(
        update(SandboxLease)
        .where(
            SandboxLease.id == lease_id,
            SandboxLease.state.in_((STATE_DESTROYING, STATE_PROVISIONING)),
        )
        .values(
            state=STATE_DESTROY_FAILED,
            # 同样清掉：失败态的候选判据不再需要它，留着只会误导下一次抢占的时间判断。
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
    """写一条命令审计。**不参与任何金额计算**：计量只有 token。

    `seq` 在同一个租约内递增，让审计可以按顺序回放「模型一步步做了什么」。取
    `max(seq)+1` 之前先锁住租约行：同一台机器上并发两条 `exec` 是真实存在的（agent
    一轮里并发调工具），只靠 `max()` 会让两条记录拿到同一个序号，回放时顺序随机。

    `output_head` / `truncated` **原样落库，不在这里再截一次**：调用方传的是渲染给模型
    看的同一份文本（头 4000 + 尾 2000，见 `app/tools/sandbox.py:_truncate`），再按头部长度
    切一刀会把**尾部**扔掉——那正是「报错的最后一行」所在的位置，也是审计最想回答的那个
    问题。体积由渲染层负责，这里只负责如实存下来。
    """
    # 锁父行而不是命令表：命令表没有「每租约一行」的锚点，锁住它等于锁住整个租约的审计。
    locked = await session.scalar(select(SandboxLease.id).where(SandboxLease.id == lease_id).with_for_update())
    if locked is None:
        # 租约行没了（被级联删除）。命令已经跑完了，审计没有可挂靠的父行；
        # 让它失败会把一次成功的执行变成错误提示，所以只告警。
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
    """刷新心跳 TTL（`SET ... EX`）。

    `redis` 由调用方传入而不是在这里 import 全局客户端：心跳与回收会跑在独立的循环/任务里，
    它们的连接生命周期和 HTTP 请求不一样，而这个函数本身没有任何理由知道这些。

    默认 TTL 取 `sandbox_idle_seconds`：一次心跳的价值就是「从现在起再给一个空闲窗口」，
    两者取同一个值，回收判定才只有一个阈值。
    """
    ttl = settings.sandbox_idle_seconds if ttl_seconds is None else ttl_seconds
    await redis.set(heartbeat_key(lease_id), "1", ex=ttl)


async def heartbeat_alive(redis: Redis, lease_id: str) -> bool:
    """心跳键是否存在 = 用户最近还在看这个会话。

    Redis 故障时**让异常抛出去**，不要返回 `False`：把「Redis 挂了」读成「所有心跳都没了」
    会让回收循环瞬间拆掉全部机器。调用方必须把异常理解为「本次不销毁」，而不是「可以销毁」。
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
