"""回收循环与租约口径的行为锁。

这一层锁三件「读代码能看懂、改错也照样跑」的事：

1. **硬上限不被软时钟否决**——`hard_ttl` 到期的候选必须无条件销毁。心跳只证明「客户端还在看」，
   而 provider 侧 `timeout=hard_ttl` 到点已经把机器杀了；用心跳续命会让租约永远停在 `ready`。
2. **单台失败只影响那一台**——导出链路抛的不是 `SandboxError`（httpx 超时、SDK 内部异常），
   漏接就会整轮中止，还会让那台机器连销毁都没走到。
3. **`destroy_failed` 占容量、进封禁清单**——它的定义是「provider 说没销毁成功」，机器一定还在跑。

零基础设施：`async_session_maker` 换成一个只当「事务边界」的空会话，Redis 与 provider 都是替身。
真实的 `find_destroyable` 需要 Postgres，不在这里验；这里验的是回收器**怎么用**它的结果。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest
from sqlalchemy.dialects import postgresql

from app.config import settings
from app.db import SandboxLease
from app.services.sandbox import lease as lease_repo
from app.services.sandbox import reaper
from app.services.sandbox.base import (
    ExecResult,
    SandboxCapacityError,
    SandboxHandle,
    SandboxSpec,
)
from app.services.sandbox.lease import (
    DESTROY_HARD_TTL,
    DESTROY_IDLE,
    LIVE_STATES,
    OPEN_STATES,
    STATE_DESTROY_FAILED,
    STATE_READY,
    DestroyCandidate,
    LeaseView,
    list_active_for_user,
)


class _NullSession:
    """回收器只把它当「事务边界」用：本文件把所有 SQL 级依赖都换成了替身。"""

    async def __aenter__(self) -> _NullSession:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _Rows:
    """`AsyncScalarResult` 的最小替身：只需要可迭代。"""

    def __init__(self, rows: list[SandboxLease]) -> None:
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class _RecordingSession:
    """记录语句、按序吐出预设结果。用来验「哪条 SQL 带上了哪些状态」，不连数据库。"""

    def __init__(self, *, counts: list[int | None] | None = None, rows: list[SandboxLease] | None = None) -> None:
        self._counts = list(counts or [])
        self._rows = list(rows or [])
        self.statements: list[object] = []

    async def scalar(self, statement: object) -> int | None:
        self.statements.append(statement)
        return self._counts.pop(0) if self._counts else 0

    async def scalars(self, statement: object) -> _Rows:
        self.statements.append(statement)
        return _Rows(self._rows)


def _sql(statement: object) -> str:
    """把语句编译成带字面量的 SQL：断言看的是「WHERE 里到底列了哪些状态」。"""
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))  # type: ignore[attr-defined]


def _lease(lease_id: str, *, ref: str = "fake-1") -> LeaseView:
    return LeaseView(
        id=lease_id,
        user_id=uuid.uuid4(),
        session_id=f"sess_{uuid.uuid4().hex}",
        provider="fake",
        provider_ref=ref,
        state=STATE_READY,
        artifact_uri=None,
        hard_expires_at=datetime.now(timezone.utc),
    )


def _candidate(lease_id: str, *, reason: str, ref: str = "fake-1") -> DestroyCandidate:
    return DestroyCandidate(lease=_lease(lease_id, ref=ref), reason=reason)


class FakeProvider:
    """只实现回收器真正用到的两个动作；`SandboxProvider` 的其余方法留空实现。"""

    name = "fake"

    def __init__(self) -> None:
        self.destroyed: list[str] = []

    async def create(self, spec: SandboxSpec) -> SandboxHandle:  # pragma: no cover - 回收不建机
        raise AssertionError("回收不该建机")

    async def exec(self, ref: str, command: str, *, timeout_s: int = 120, workdir: str | None = None, owner_tag=None):  # noqa: ANN001, ANN201, ARG002
        return ExecResult(exit_code=0, stdout="", stderr="", duration_ms=1)

    async def put_file(self, ref: str, path: str, data: bytes, *, owner_tag=None) -> None:  # noqa: ANN001, ARG002
        return None

    async def get_file(self, ref: str, path: str, *, max_bytes: int, owner_tag=None) -> bytes:  # noqa: ANN001, ARG002
        return b""

    async def destroy(self, ref: str, *, owner_tag=None) -> None:  # noqa: ANN001, ARG002
        self.destroyed.append(ref)

    async def is_alive(self, ref: str) -> bool:  # pragma: no cover - 回收不核对存活
        return True


@dataclass
class ReapHarness:
    """一轮回收的全部替身与记录。行为按需注入，避免每个用例各写一份 monkeypatch。"""

    candidates: list[DestroyCandidate] = field(default_factory=list)
    heartbeats: set[str] = field(default_factory=set)
    export_raises: dict[str, BaseException] = field(default_factory=dict)
    mark_destroying_raises: set[str] = field(default_factory=set)
    provider: FakeProvider = field(default_factory=FakeProvider)
    #: `mark_destroyed` 落下的 (lease_id, reason, artifact_uri)
    destroyed: list[tuple[str, str, str | None]] = field(default_factory=list)
    #: `mark_destroy_failed` 落下的 (lease_id, error)
    failed_marks: list[tuple[str, str]] = field(default_factory=list)
    marked_destroying: list[str] = field(default_factory=list)

    async def find_destroyable(self, session, *, now=None, idle_seconds=None):  # noqa: ANN001, ANN201, ARG002
        return self.candidates

    async def find_stale_provisioning(self, session, *, older_than):  # noqa: ANN001, ANN201, ARG002
        return []

    async def heartbeat_alive(self, redis, lease_id: str) -> bool:  # noqa: ANN001, ARG002
        return lease_id in self.heartbeats

    async def mark_destroying(self, session, lease_id: str, *, now=None) -> bool:  # noqa: ANN001, ANN201, ARG002
        if lease_id in self.mark_destroying_raises:
            raise RuntimeError("database is down")
        self.marked_destroying.append(lease_id)
        return True

    async def mark_destroyed(self, session, lease_id: str, *, reason: str, artifact_uri=None) -> None:  # noqa: ANN001, ARG002
        self.destroyed.append((lease_id, reason, artifact_uri))

    async def mark_destroy_failed(self, session, lease_id: str, *, error: str, reason: str = "error") -> None:  # noqa: ANN001, ARG002
        self.failed_marks.append((lease_id, error))

    async def export_workspace(self, provider, lease: LeaseView, *, workdir: str) -> str | None:  # noqa: ANN001, ARG002
        error = self.export_raises.get(lease.id)
        if error is not None:
            raise error
        return None


@pytest.fixture
def reap(monkeypatch) -> ReapHarness:
    """把回收器依赖的 DB / Redis / provider / 导出链路全部换成替身。"""
    harness = ReapHarness()
    monkeypatch.setattr(reaper, "async_session_maker", _NullSession)
    monkeypatch.setattr(reaper, "get_redis", lambda: object())
    monkeypatch.setattr(reaper, "find_destroyable", harness.find_destroyable)
    monkeypatch.setattr(reaper, "find_stale_provisioning", harness.find_stale_provisioning)
    monkeypatch.setattr(reaper, "heartbeat_alive", harness.heartbeat_alive)
    monkeypatch.setattr(reaper, "mark_destroying", harness.mark_destroying)
    monkeypatch.setattr(reaper, "mark_destroyed", harness.mark_destroyed)
    monkeypatch.setattr(reaper, "mark_destroy_failed", harness.mark_destroy_failed)
    monkeypatch.setattr(reaper, "export_workspace", harness.export_workspace)
    return harness


# ---------------------------------------------------------------------
# ① 硬上限到期：心跳不能否决
# ---------------------------------------------------------------------
async def test_hard_ttl_lease_is_destroyed_even_when_heartbeat_is_alive(reap: ReapHarness):
    """开着标签页的用户也会到硬上限。

    provider 侧 `timeout=hard_ttl` 到点已经把机器杀掉，此时心跳仍然活着；用心跳否决就留下一条
    `ready` 且 `provider_ref` 指向不存在机器的租约，所有沙箱工具永久失败且提示「请重试」。
    """
    reap.candidates = [_candidate("sbx_expired", reason=DESTROY_HARD_TTL)]
    reap.heartbeats = {"sbx_expired"}

    summary = await reaper.reap_once(provider=reap.provider)

    assert summary.skipped_heartbeat == 0
    assert summary.destroyed == 1
    # 原因必须原样传到终态：界面据此说「到硬上限了」，而不是含糊的「坏了」。
    assert reap.destroyed == [("sbx_expired", DESTROY_HARD_TTL, None)]
    assert reap.provider.destroyed == ["fake-1"]


async def test_idle_lease_with_live_heartbeat_is_skipped(reap: ReapHarness):
    """硬上限之前心跳仍然是一票否决：用户还开着页面，机器不该被回收。"""
    reap.candidates = [_candidate("sbx_idle", reason=DESTROY_IDLE)]
    reap.heartbeats = {"sbx_idle"}

    summary = await reaper.reap_once(provider=reap.provider)

    assert summary.skipped_heartbeat == 1
    assert summary.destroyed == 0
    assert reap.marked_destroying == []
    assert reap.provider.destroyed == []


# ---------------------------------------------------------------------
# ② 单台失败只影响那一台
# ---------------------------------------------------------------------
async def test_export_failure_still_destroys_the_machine(reap: ReapHarness):
    """导出链路抛的不是 `SandboxError`（httpx 超时、SDK 内部异常）。

    产物是尽力而为，机器必须回收：漏接会让这台机器连 `destroy` 都没走到，停在 `destroying`
    里等 15 分钟重抢，期间一直占着该用户唯一的容量位。
    """
    reap.candidates = [_candidate("sbx_a", reason=DESTROY_IDLE)]
    reap.export_raises["sbx_a"] = RuntimeError("httpx.ReadTimeout")

    summary = await reaper.reap_once(provider=reap.provider)

    assert summary.failed == 0
    assert reap.destroyed == [("sbx_a", DESTROY_IDLE, None)]
    assert reap.provider.destroyed == ["fake-1"]
    assert reap.failed_marks == [], "机器已经拆掉了，不该落成 destroy_failed"


async def test_one_machine_failure_does_not_abort_the_round(reap: ReapHarness):
    """第一台抛未预期异常时，第二轮候选不能被一起放弃。"""
    reap.candidates = [
        _candidate("sbx_bad", reason=DESTROY_IDLE, ref="fake-1"),
        _candidate("sbx_good", reason=DESTROY_IDLE, ref="fake-2"),
    ]
    reap.mark_destroying_raises = {"sbx_bad"}

    summary = await reaper.reap_once(provider=reap.provider)

    assert summary.failed == 1
    assert summary.destroyed == 1
    assert [row[0] for row in reap.destroyed] == ["sbx_good"]
    assert reap.provider.destroyed == ["fake-2"]


# ---------------------------------------------------------------------
# ③ `destroy_failed` 占容量、进封禁清单
# ---------------------------------------------------------------------
async def test_capacity_counts_destroy_failed_machines():
    """容量口径必须覆盖「provider 没确认释放」的状态。

    漏掉 `destroy_failed` 的后果是：reset 失败一次 → 下一次 exec 立刻再建一台 → 同一用户两台机器，
    全局上限同时失效（节点池被打爆）。
    """
    assert STATE_DESTROY_FAILED in OPEN_STATES
    assert STATE_DESTROY_FAILED not in LIVE_STATES, "LIVE_STATES 喂部分唯一索引，不能让该用户开不出新机器"

    session = _RecordingSession(counts=[0, 0])
    await lease_repo._reject_if_at_capacity(session, uuid.uuid4())  # noqa: SLF001 - 容量判定本身就是要锁的行为

    assert len(session.statements) == 2, "每用户一次 + 全站一次"
    for statement in session.statements:
        assert "'destroy_failed'" in _sql(statement)


async def test_per_user_capacity_still_rejects_over_the_limit():
    """口径加上 `destroy_failed` 之后，超限仍然是直接拒绝而不是静默放行。"""
    session = _RecordingSession(counts=[settings.sandbox_max_per_user, 0])

    with pytest.raises(SandboxCapacityError):
        await lease_repo._reject_if_at_capacity(session, uuid.uuid4())  # noqa: SLF001


async def test_ban_list_includes_destroy_failed_machines():
    """封禁时要拆的清单同样走 `OPEN_STATES`：漏掉 `destroy_failed` 的那台机器会继续跑。"""
    user_id = uuid.uuid4()
    row = SandboxLease(
        id="sbx_left_behind",
        user_id=user_id,
        session_id="sess_x",
        provider="fake",
        provider_ref="fake-1",
        template="tpl",
        template_version="1",
        state=STATE_DESTROY_FAILED,
        hard_expires_at=datetime.now(timezone.utc),
    )
    session = _RecordingSession(rows=[row])

    leases = await list_active_for_user(session, user_id=user_id)

    assert [lease.id for lease in leases] == ["sbx_left_behind"]
    assert "'destroy_failed'" in _sql(session.statements[0])
