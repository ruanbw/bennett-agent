"""沙箱租约状态机的行为锁（`app/services/sandbox/lease.py`）。

这里验的是**线上花了钱才会暴露**的那几条性质，不是函数返回值长什么样：

1. 同会话只建一台机器——agent 一轮里并发调两个工具是真实存在的；
2. 容量超限要**明确拒绝**，不能「先建了再说」（§8：不静默排队）；
3. 销毁权只能被抢一次——先 SELECT 再 UPDATE 会让回收把机器从长任务下面抽走（§11.6）；
4. 建机失败留一行 `failed`，不是删掉，也不是卡在 `provisioning`；
5. 命令审计的 `seq` 单调，能按顺序回放；
6. 回收候选覆盖两个时钟（idle / hard_ttl），并且**不**包含正在用或已销毁的租约。

用真实 Postgres（`tests/conftest.py` 的共享测试库），provider 是一个只实现
`create` 的替身：本文件验的是租约状态机，不是容器。**心跳**（Redis）不在这里验——
`find_destroyable` 只给候选集，Redis 那一半判定属于回收循环。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app.config import settings
from app.db import SandboxCommand, SandboxLease, User, async_session_maker
from app.services.sandbox.base import (
    MAX_OUTPUT_HEAD_CHARS,
    SandboxCapacityError,
    SandboxHandle,
    SandboxSpec,
    owner_tag,
)
from app.services.sandbox.lease import (
    DESTROY_HARD_TTL,
    DESTROY_IDLE,
    STATE_DESTROY_FAILED,
    STATE_DESTROYED,
    STATE_DESTROYING,
    STATE_FAILED,
    STATE_PAUSED,
    STATE_READY,
    LeaseView,
    ensure_lease,
    find_destroyable,
    get_active_lease,
    mark_destroy_failed,
    mark_destroyed,
    mark_destroying,
    record_command,
)


class FakeProvider:
    """provider 替身：只记录 `create` 被调用了几次、给了什么 spec。"""

    name = "docker"

    def __init__(self) -> None:
        self.specs: list[SandboxSpec] = []
        self.fail_with: BaseException | None = None

    async def create(self, spec: SandboxSpec) -> SandboxHandle:
        self.specs.append(spec)
        if self.fail_with is not None:
            raise self.fail_with
        # ref 用序号而不是随机值：用例要断言「第一台机器的 ref」。
        return SandboxHandle(ref=f"c-{len(self.specs)}", ready_ms=1)


async def _create_user(email: str | None = None) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with async_session_maker() as session:
        session.add(
            User(
                id=user_id,
                email=email or f"{user_id}@example.com",
                hashed_password="x",
                is_active=True,
                is_superuser=False,
                is_verified=True,
            )
        )
        await session.commit()
    return user_id


@pytest.fixture
async def user_id() -> uuid.UUID:
    return await _create_user()


async def _ensure(
    user_id: uuid.UUID,
    session_id: str,
    provider: FakeProvider,
    **kwargs: object,
) -> tuple[LeaseView, bool]:
    """每次调用都用独立会话：`ensure_lease` 自己提交，复用会话会掩盖跨请求可见性问题。

    `kwargs` 目前只用来注入 `now`（让回收用例能在不睡几小时的前提下造出旧租约）。
    """
    async with async_session_maker() as session:
        return await ensure_lease(session, user_id=user_id, session_id=session_id, create=provider.create, **kwargs)


async def _lease_row(lease_id: str) -> SandboxLease:
    async with async_session_maker() as session:
        row = await session.get(SandboxLease, lease_id)
        assert row is not None
        return row


# ---------------------------------------------------------------------
# ① 同会话幂等
# ---------------------------------------------------------------------
async def test_same_session_creates_exactly_one_machine(user_id):
    """两次 ensure 只建一台机器，create 只被调用一次。"""
    provider = FakeProvider()

    first, created_first = await _ensure(user_id, "sess_a", provider)
    second, created_second = await _ensure(user_id, "sess_a", provider)

    assert (created_first, created_second) == (True, False)
    assert first.id == second.id
    assert first.state == STATE_READY
    # provider_ref 必须落库：它只在服务端流动，丢了就再也找不到那台机器
    assert first.provider_ref == "c-1"
    assert second.provider_ref == "c-1"
    assert len(provider.specs) == 1
    assert provider.specs[0].lease_id == first.id
    # 建机时把配置与归属标记一次带齐（owner_tag 是机器侧的第二道证人）
    assert provider.specs[0].template == settings.sandbox_template
    assert provider.specs[0].owner_tag == owner_tag(user_id)


async def test_concurrent_ensure_still_creates_one_machine(user_id):
    """两路同时 ensure（agent 一轮里并发调两个工具）：只有一路建机，另一路读回同一行。

    这条走的是「INSERT 撞部分唯一索引 → 回滚 savepoint → 重读」这条分支。没有它，
    唯一的并发保护就只剩索引本身，而索引冲突会以 `IntegrityError` 冒到工具层。
    """
    provider = FakeProvider()

    async def ensure_once():
        return await _ensure(user_id, "sess_a", provider)

    first, second = await asyncio.gather(ensure_once(), ensure_once())

    assert sorted([first[1], second[1]]) == [False, True]
    assert first[0].id == second[0].id
    assert len(provider.specs) == 1
    async with async_session_maker() as session:
        rows = (await session.scalars(select(SandboxLease).where(SandboxLease.user_id == user_id))).all()
    assert len(rows) == 1


async def test_lease_lookup_is_scoped_by_user(user_id):
    """同一个 session_id 在别人名下读不到任何东西（授权由构造保证，读也不能漏）。"""
    provider = FakeProvider()
    await _ensure(user_id, "sess_shared", provider)

    other_user = await _create_user()
    async with async_session_maker() as session:
        assert await get_active_lease(session, user_id=other_user) is None
        assert await get_active_lease(session, user_id=user_id) is not None


# ---------------------------------------------------------------------
# ② 容量
# ---------------------------------------------------------------------
async def test_second_session_reuses_instead_of_rejecting(user_id, monkeypatch):
    """默认 `max_per_user=1` 时，第二个会话拿到的是**同一台**机器，不是容量错误。

    这曾经是一条「第二个会话被拒绝」的用例——那是「一会话一台」时代的语义。
    改成每用户一台之后，用户在会话间切换不该撞上限；`max_per_user` 退化成配置错误
    时的安全阀（被人为调到 0 才算异常，而 `ge=1` 已经挡住了）。
    """
    monkeypatch.setattr(settings, "sandbox_max_per_user", 1)
    provider = FakeProvider()
    first, created_first = await _ensure(user_id, "sess_a", provider)
    second, created_second = await _ensure(user_id, "sess_b", provider)

    assert first.id == second.id
    assert (created_first, created_second) == (True, False)
    assert len(provider.specs) == 1
    # 归因列跟着最新会话走
    assert second.session_id == "sess_b"


async def test_rejected_when_node_pool_is_full(monkeypatch):
    """全局并发上限是节点池容量，不是付费分级：满了就拒绝，不排队。"""
    monkeypatch.setattr(settings, "sandbox_max_per_user", 5)
    monkeypatch.setattr(settings, "sandbox_max_concurrent", 1)
    provider = FakeProvider()
    await _ensure(await _create_user(), "sess_a", provider)

    with pytest.raises(SandboxCapacityError) as excinfo:
        await _ensure(await _create_user(), "sess_b", provider)

    assert "资源已满" in str(excinfo.value)
    assert len(provider.specs) == 1


# ---------------------------------------------------------------------
# ③ 销毁抢占
# ---------------------------------------------------------------------
async def test_mark_destroying_is_claimed_by_exactly_one_caller(user_id):
    """两个回收者同时抢同一台机器：只有一个成功，另一个看到 `destroying`。"""
    provider = FakeProvider()
    lease, _ = await _ensure(user_id, "sess_a", provider)

    async def claim() -> bool:
        async with async_session_maker() as session:
            return await mark_destroying(session, lease.id)

    results = await asyncio.gather(claim(), claim())

    assert sorted(results) == [False, True]
    row = await _lease_row(lease.id)
    assert row.state == STATE_DESTROYING
    # 抢到之后不能再被任何人抢：销毁中的机器不接受第二次销毁权
    async with async_session_maker() as session:
        assert await mark_destroying(session, lease.id) is False


async def test_destroy_failed_can_be_reclaimed(user_id):
    """销毁失败后必须能重新抢占。

    这是最容易写错的一条：把 `destroy_failed` 排除在可抢占状态外，等于把「失败」
    变成终态——机器还在跑、还在占内存，却没有任何人会再去关它（永久泄漏）。
    """
    provider = FakeProvider()
    lease, _ = await _ensure(user_id, "sess_retry", provider)

    async with async_session_maker() as session:
        assert await mark_destroying(session, lease.id) is True
    async with async_session_maker() as session:
        await mark_destroy_failed(session, lease.id, error="provider timeout")
    row = await _lease_row(lease.id)
    assert row.state == STATE_DESTROY_FAILED

    # 失败不是终态：回收循环下一轮要能重新拿到它
    async with async_session_maker() as session:
        assert await mark_destroying(session, lease.id) is True
    row = await _lease_row(lease.id)
    assert row.state == STATE_DESTROYING


# ---------------------------------------------------------------------
# ④ 建机失败
# ---------------------------------------------------------------------
async def test_create_failure_marks_lease_failed_and_reraises(user_id):
    """create 抛异常 → 行标 failed + 原因，异常原样抛出，并且能重新建一台。"""
    provider = FakeProvider()
    provider.fail_with = RuntimeError("docker daemon 连不上")

    with pytest.raises(RuntimeError, match="docker daemon 连不上"):
        await _ensure(user_id, "sess_a", provider)

    async with async_session_maker() as session:
        rows = (await session.scalars(select(SandboxLease).where(SandboxLease.user_id == user_id))).all()
    assert [row.state for row in rows] == [STATE_FAILED]
    failed = rows[0]
    # 失败原因要能对上「哪一类失败」：排障时类型名比一句自然语言有用
    assert "RuntimeError: docker daemon 连不上" in (failed.error or "")
    # failed 不占活动名额：下一次 ensure 能重新建机（用户点「重试」必须有效）
    async with async_session_maker() as session:
        assert await get_active_lease(session, user_id=user_id) is None

    provider.fail_with = None
    retried, created = await _ensure(user_id, "sess_a", provider)
    assert created is True
    assert retried.id != failed.id
    assert retried.state == STATE_READY


# ---------------------------------------------------------------------
# ⑤ 命令审计
# ---------------------------------------------------------------------
async def test_record_command_assigns_increasing_seq(user_id):
    """seq 从 1 开始单调递增；审计存下的就是渲染层给的那份文本（不二次截断）。"""
    provider = FakeProvider()
    lease, _ = await _ensure(user_id, "sess_a", provider)

    for index in range(3):
        async with async_session_maker() as session:
            await record_command(
                session,
                lease.id,
                command=f"echo {index}",
                exit_code=0,
                duration_ms=5,
                output_bytes=3,
                output_head=f"{index}\n",
                truncated=False,
            )

    # 渲染层给的是「头 4000 + 省略标记 + 尾 2000」（`app/tools/sandbox.py:_truncate`）。
    # 审计必须原样存下来：按 MAX_OUTPUT_HEAD_CHARS 再切一刀会把尾部扔掉，
    # 而尾部正是报错最后一行所在的位置。
    rendered = "x" * MAX_OUTPUT_HEAD_CHARS + "\n……（中间省略 900 个字符）……\n" + "最后一行：boom"
    async with async_session_maker() as session:
        await record_command(
            session,
            lease.id,
            command="cat big.log",
            exit_code=0,
            duration_ms=7,
            output_bytes=50_000,
            output_head=rendered,
            truncated=True,
        )

    async with async_session_maker() as session:
        rows = (
            await session.scalars(
                select(SandboxCommand).where(SandboxCommand.lease_id == lease.id).order_by(SandboxCommand.seq)
            )
        ).all()

    assert [row.seq for row in rows] == [1, 2, 3, 4]
    assert [row.command for row in rows] == ["echo 0", "echo 1", "echo 2", "cat big.log"]
    assert rows[3].output_head == rendered
    assert rows[3].truncated is True
    assert rows[2].truncated is False


# ---------------------------------------------------------------------
# ⑥ 回收候选：两个时钟
# ---------------------------------------------------------------------
async def test_find_destroyable_covers_idle_and_hard_ttl(user_id, monkeypatch):
    """hard_ttl 与 idle 两条路径都能产出候选，且原因归到正确的时钟。

    `find_destroyable` 只看数据库；心跳（Redis）是调用方的第二半判定，不在这个用例里。
    """
    monkeypatch.setattr(settings, "sandbox_max_per_user", 10)
    provider = FakeProvider()
    base = datetime.now(timezone.utc)
    idle_seconds = 600

    # 四个状态用**四个用户**造：机器是按用户分的，同一用户只会有一台。
    # 用 session_id 当区分键已经不成立（复用会把它改写成最新会话）。
    hard, _ = await _ensure(await _create_user(), "sess_hard", provider, now=base - timedelta(hours=5))
    idle, _ = await _ensure(await _create_user(), "sess_idle", provider, now=base - timedelta(hours=1))
    fresh, _ = await _ensure(await _create_user(), "sess_fresh", provider, now=base)
    paused, _ = await _ensure(await _create_user(), "sess_paused", provider, now=base - timedelta(hours=1))
    async with async_session_maker() as session:
        await session.execute(update(SandboxLease).where(SandboxLease.id == paused.id).values(state=STATE_PAUSED))
        await session.commit()
    # 已经销毁的租约不该再进候选（否则回收循环会对一台不存在的机器反复告警）
    dead, _ = await _ensure(await _create_user(), "sess_dead", provider, now=base - timedelta(hours=5))
    async with async_session_maker() as session:
        assert await mark_destroying(session, dead.id) is True
    async with async_session_maker() as session:
        await mark_destroyed(session, dead.id, reason=DESTROY_IDLE)

    # 只让 hard_ttl 生效：把 idle 阈值放大到不可能触发
    async with async_session_maker() as session:
        hard_only = await find_destroyable(session, now=base, idle_seconds=10_000_000)
    assert {candidate.lease.id: candidate.reason for candidate in hard_only} == {hard.id: DESTROY_HARD_TTL}

    async with async_session_maker() as session:
        candidates = await find_destroyable(session, now=base, idle_seconds=idle_seconds)
    found = {candidate.lease.id: candidate.reason for candidate in candidates}
    assert found == {
        hard.id: DESTROY_HARD_TTL,
        idle.id: DESTROY_IDLE,
        paused.id: DESTROY_IDLE,
    }
    assert fresh.id not in {candidate.lease.id for candidate in candidates}
    assert dead.id not in {candidate.lease.id for candidate in candidates}

    row = await _lease_row(dead.id)
    assert row.state == STATE_DESTROYED
    assert row.destroy_reason == DESTROY_IDLE
    assert row.destroyed_at is not None


async def test_stalled_destroying_lease_can_be_reclaimed(user_id):
    """卡在 `destroying` 的租约必须能被回收循环接手。

    这个状态没有出路时，它会永久吃掉该用户**唯一**的容量位
    （`_reject_if_at_capacity` 把 `destroying` 算进 `OPEN_STATES`），而
    `get_active_lease` 又不认它——用户看到的只有「执行环境资源已满」，除了人工改库
    没有别的办法。触发窗口很宽：进程在「抢占已提交」与「终态已提交」之间被
    SIGKILL/OOM/滚动发布杀掉，中间还夹着 120 秒打包与最多 200MB 读回。
    """
    from datetime import datetime, timedelta, timezone

    from app.db import SandboxLease
    from app.services.sandbox.lease import DESTROYING_STALE_SECONDS

    provider = FakeProvider()
    lease, _ = await _ensure(user_id, "sess_stalled", provider)

    async with async_session_maker() as session:
        assert await mark_destroying(session, lease.id) is True

    # 刚抢下时不能重入（这正是「抢占唯一」的含义）
    async with async_session_maker() as session:
        assert await mark_destroying(session, lease.id) is False

    # 时间推过门槛：那是一次**中断的**销毁，必须能被接手
    stale_at = datetime.now(timezone.utc) + timedelta(seconds=DESTROYING_STALE_SECONDS + 1)
    async with async_session_maker() as session:
        assert await mark_destroying(session, lease.id, now=stale_at) is True
        row = await session.get(SandboxLease, lease.id)
    assert row.state == STATE_DESTROYING
    assert row.destroying_since is not None


async def test_stalled_destroying_counts_as_a_reclaim_candidate(user_id):
    """`find_destroyable` 要把它当候选，否则回收循环根本看不到它。"""
    from datetime import datetime, timedelta, timezone

    from app.services.sandbox.lease import DESTROYING_STALE_SECONDS, find_destroyable

    provider = FakeProvider()
    lease, _ = await _ensure(user_id, "sess_stalled2", provider)

    async with async_session_maker() as session:
        assert await mark_destroying(session, lease.id) is True

    # 刚进入 destroying：不是候选（正常销毁进行中）
    async with async_session_maker() as session:
        fresh = await find_destroyable(session)
    assert all(candidate.lease.id != lease.id for candidate in fresh)

    # 超过门槛：成为候选
    stale_at = datetime.now(timezone.utc) + timedelta(seconds=DESTROYING_STALE_SECONDS + 1)
    async with async_session_maker() as session:
        stale = await find_destroyable(session, now=stale_at)
    assert [candidate.lease.id for candidate in stale] == [lease.id]


async def test_mark_destroyed_clears_destroying_since(user_id):
    """终态必须清掉 `destroying_since`，否则下一次销毁的 age 门槛会永远判成立。"""
    from app.db import SandboxLease

    provider = FakeProvider()
    lease, _ = await _ensure(user_id, "sess_clear", provider)
    async with async_session_maker() as session:
        assert await mark_destroying(session, lease.id) is True
    async with async_session_maker() as session:
        await mark_destroyed(session, lease.id, reason=DESTROY_IDLE)

    async with async_session_maker() as session:
        row = await session.get(SandboxLease, lease.id)
    assert row.state == STATE_DESTROYED
    assert row.destroying_since is None
