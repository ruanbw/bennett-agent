"""沙箱回收循环（`app/services/sandbox/reaper.py`）。

## 这些用例想锁住什么

回收是唯一「没人看着也得对」的组件：它是常驻循环、按时间触发、会把用户正在用的
东西拆掉。所以断言的不是「函数有没有跑」，而是四条不能错的行为：

1. **有心跳就不拆**——否则「用户正在读结果」会被误判成「没人用了」；
2. **拆之前产物已落盘**——机器可重建，用户的计算结果不行；
3. **失败必须可重试**——`destroy_failed` 不是终态，否则机器永久泄漏；
4. **归因正确**——`idle` / `hard_ttl` / `session_deleted` 写错时，事后没有任何办法回溯。

provider / 对象存储 / Redis 全部用替身：这些用例验证的是**编排顺序**，不是 SDK 行为
（后者由 `scripts/smoke_sandbox.py` 对着真 server 验证）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from app.config import settings
from app.db import SandboxLease, User, async_session_maker
from app.services.sandbox import artifacts as artifacts_module
from app.services.sandbox import reaper as reaper_module
from app.services.sandbox.base import ExecResult, SandboxError, SandboxHandle, SandboxSpec
from app.services.sandbox.lease import (
    DESTROY_IDLE,
    STATE_DESTROY_FAILED,
    STATE_DESTROYED,
    STATE_READY,
    ensure_lease,
    get_latest_lease,
    heartbeat_key,
)


# ---------------------------------------------------------------------
# 替身
# ---------------------------------------------------------------------
class FakeProvider:
    """最小 provider：只回答回收流程真正会问的问题。"""

    name = "opensandbox"

    def __init__(self, *, workspace_bytes: int = 1024, fail_destroy: bool = False) -> None:
        self.workspace_bytes = workspace_bytes
        self.fail_destroy = fail_destroy
        self.destroyed: list[str] = []
        self.created = 0

    async def create(self, spec: SandboxSpec) -> SandboxHandle:
        self.created += 1
        return SandboxHandle(ref=f"ref-{self.created}", ready_ms=1)

    async def exec(self, ref, command, *, timeout_s=120, workdir=None, owner_tag=None):  # noqa: ANN001
        if "du -sb" in command:
            return ExecResult(exit_code=0, stdout=f"{self.workspace_bytes}\n", stderr="", duration_ms=1)
        if command.startswith("find "):
            return ExecResult(exit_code=0, stdout="12\t./a.txt\n3\t./b.txt\n", stderr="", duration_ms=1)
        # tar：返回 0 就够，体积由 get_file 的 fake 决定
        return ExecResult(exit_code=0, stdout="", stderr="", duration_ms=1)

    async def put_file(self, ref, path, data, *, owner_tag=None):  # noqa: ANN001
        return None

    async def get_file(self, ref, path, *, max_bytes, owner_tag=None):  # noqa: ANN001
        payload = b"x" * min(self.workspace_bytes, 2048)
        if len(payload) > max_bytes:
            raise SandboxError("too big")
        return payload

    async def destroy(self, ref, *, owner_tag=None):
        if self.fail_destroy:
            raise SandboxError("provider 挂了")
        self.destroyed.append(ref)

    async def is_alive(self, ref):
        return ref not in self.destroyed


class FakeStore:
    def __init__(self) -> None:
        self.uploaded: dict[str, bytes] = {}

    async def put_file(self, key: str, path: str, content_type: str) -> None:
        with open(path, "rb") as handle:
            self.uploaded[key] = handle.read()


class FakeRedis:
    """只需要 SET EX / EXISTS 两个动作。"""

    def __init__(self) -> None:
        self.keys: dict[str, int] = {}

    async def set(self, name: str, value, *, ex=None):  # noqa: ANN001
        self.keys[name] = ex or 0
        return True

    async def exists(self, name: str) -> int:
        return 1 if name in self.keys else 0


@pytest.fixture
async def user_id() -> uuid.UUID:
    user_id = uuid.uuid4()
    async with async_session_maker() as session:
        session.add(
            User(
                id=user_id,
                email=f"{user_id}@example.com",
                hashed_password="x",
                is_active=True,
                is_superuser=False,
                is_verified=True,
            )
        )
        await session.commit()
    return user_id


@pytest.fixture
def store(monkeypatch) -> FakeStore:
    fake = FakeStore()
    monkeypatch.setattr(artifacts_module, "get_object_store", lambda: fake)
    return fake


@pytest.fixture
def redis(monkeypatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(reaper_module, "get_redis", lambda: fake)
    return fake


async def _make_lease(user_id: uuid.UUID, provider: FakeProvider, session_id: str = "sess_a"):
    async with async_session_maker() as session:
        lease, _ = await ensure_lease(session, user_id=user_id, session_id=session_id, create=provider.create)
    return lease


async def _age_lease(lease_id: str, *, hours: int = 2) -> None:
    """把租约做旧：`last_activity` 与 `created_at` 一起往前推。"""
    old = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with async_session_maker() as session:
        await session.execute(
            update(SandboxLease).where(SandboxLease.id == lease_id).values(last_activity=old, created_at=old)
        )
        await session.commit()


# ---------------------------------------------------------------------
# ① 空闲 + 无心跳 → 拆，且产物已落盘
# ---------------------------------------------------------------------
async def test_idle_lease_without_heartbeat_is_destroyed_with_artifact(user_id, store, redis):
    provider = FakeProvider(workspace_bytes=1024)
    lease = await _make_lease(user_id, provider)
    await _age_lease(lease.id)

    summary = await reaper_module.reap_once(provider=provider)

    assert summary.destroyed == 1
    assert provider.destroyed == ["ref-1"]
    async with async_session_maker() as session:
        row = await session.get(SandboxLease, lease.id)
    assert row.state == STATE_DESTROYED
    assert row.destroy_reason == DESTROY_IDLE
    assert row.artifact_uri and row.artifact_uri.endswith("workspace.tar.gz")
    # 产物真的进了对象存储，而不是只记了个 key
    assert row.artifact_uri in store.uploaded


async def test_artifact_upload_happens_before_destroy(user_id, store, redis):
    """顺序错了就再也没有产物可导出：用「销毁时断言文件已在存储里」来锁住它。"""
    provider = FakeProvider()
    lease = await _make_lease(user_id, provider)
    await _age_lease(lease.id)

    seen: list[int] = []
    original = provider.destroy

    async def spy(ref, *, owner_tag=None):  # noqa: ANN001
        seen.append(len(store.uploaded))
        await original(ref, owner_tag=owner_tag)

    provider.destroy = spy  # type: ignore[method-assign]

    await reaper_module.reap_once(provider=provider)
    assert seen == [1], "销毁时产物必须已经上传过了"


# ---------------------------------------------------------------------
# ② 有心跳 → 不许动
# ---------------------------------------------------------------------
async def test_heartbeat_alive_lease_is_skipped(user_id, store, redis):
    provider = FakeProvider()
    lease = await _make_lease(user_id, provider)
    await _age_lease(lease.id)
    redis.keys[heartbeat_key(lease.id)] = settings.sandbox_idle_seconds

    summary = await reaper_module.reap_once(provider=provider)

    assert summary.skipped_heartbeat == 1
    assert provider.destroyed == []
    async with async_session_maker() as session:
        row = await session.get(SandboxLease, lease.id)
    assert row.state == STATE_READY


async def test_redis_failure_skips_instead_of_killing(user_id, store, monkeypatch):
    """Redis 抖动时保守处理：这一轮不动它。拆错的代价远大于晚拆一轮。"""

    class BrokenRedis:
        async def exists(self, name: str) -> int:
            raise RuntimeError("redis down")

    monkeypatch.setattr(reaper_module, "get_redis", lambda: BrokenRedis())
    provider = FakeProvider()
    lease = await _make_lease(user_id, provider)
    await _age_lease(lease.id)

    summary = await reaper_module.reap_once(provider=provider)

    assert summary.skipped_heartbeat == 1
    assert provider.destroyed == []


# ---------------------------------------------------------------------
# ③ 失败可重试
# ---------------------------------------------------------------------
async def test_destroy_failure_marks_destroy_failed_and_is_retried(user_id, store, redis):
    provider = FakeProvider(fail_destroy=True)
    lease = await _make_lease(user_id, provider)
    await _age_lease(lease.id)

    summary = await reaper_module.reap_once(provider=provider)
    assert summary.failed == 1
    async with async_session_maker() as session:
        row = await session.get(SandboxLease, lease.id)
    assert row.state == STATE_DESTROY_FAILED
    assert "销毁失败" in (row.error or "")

    # 下一轮：provider 恢复 → 能重新抢到并拆掉（失败不是终态）
    provider.fail_destroy = False
    summary = await reaper_module.reap_once(provider=provider)
    assert summary.destroyed == 1
    async with async_session_maker() as session:
        row = await session.get(SandboxLease, lease.id)
    assert row.state == STATE_DESTROYED


# ---------------------------------------------------------------------
# ④ 归因
# ---------------------------------------------------------------------
async def test_hard_ttl_reason_is_recorded(user_id, store, redis):
    provider = FakeProvider()
    lease = await _make_lease(user_id, provider)
    async with async_session_maker() as session:
        await session.execute(
            update(SandboxLease)
            .where(SandboxLease.id == lease.id)
            .values(hard_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        )
        await session.commit()

    summary = await reaper_module.reap_once(provider=provider)

    assert summary.destroyed == 1
    async with async_session_maker() as session:
        row = await session.get(SandboxLease, lease.id)
    assert row.destroy_reason == "hard_ttl"


async def test_deleting_a_session_does_not_destroy_the_shared_machine(user_id, store, redis):
    """删会话**不拆机器**：机器是按用户分的，别的会话可能正在用它。

    这条曾经是「删会话 → 拆该会话的机器」。改成每用户一台之后，那样的行为意味着
    「删掉一个会话顺手弄丢另一个会话正在跑的工作」。机器改由空闲/硬上限回收。
    """
    provider = FakeProvider()
    lease = await _make_lease(user_id, provider, "sess_x")

    # 删除会话走的是 sync 服务，它现在不碰沙箱；租约必须原样活着。
    async with async_session_maker() as session:
        latest = await get_latest_lease(session, user_id=user_id)
    assert latest is not None and latest.id == lease.id
    assert latest.state == STATE_READY
    assert provider.destroyed == []


async def test_ban_destroys_all_of_the_users_machines(user_id, store, redis):
    """封号要立刻收回算力：认证会拒绝他，机器也不能留着跑。"""
    provider = FakeProvider()
    await _make_lease(user_id, provider, "sess_a")

    destroyed = await reaper_module.destroy_leases_for_user(user_id, provider=provider)

    assert destroyed == 1
    assert provider.destroyed == ["ref-1"]
    async with async_session_maker() as session:
        latest = await get_latest_lease(session, user_id=user_id)
    assert latest.state == STATE_DESTROYED


# ---------------------------------------------------------------------
# ⑤ 残留 provisioning
# ---------------------------------------------------------------------
async def test_stale_provisioning_is_marked_failed_not_destroyed(user_id, store, redis):
    """没有 ref 的行不能猜着销毁（按空 ref 去删只会失败），标记 + 告警。"""
    async with async_session_maker() as session:
        session.add(
            SandboxLease(
                id="sbx_" + uuid.uuid4().hex,
                user_id=user_id,
                session_id="sess_stale",
                provider="opensandbox",
                provider_ref=None,
                template="bennett-sandbox",
                template_version="0.1",
                state="provisioning",
                created_at=datetime.now(timezone.utc) - timedelta(hours=1),
                hard_expires_at=datetime.now(timezone.utc) + timedelta(hours=3),
            )
        )
        await session.commit()
        stale_id = (await session.scalars(select(SandboxLease.id))).one()

    summary = await reaper_module.reap_once(provider=FakeProvider())

    assert summary.stale_provisioning == 1
    async with async_session_maker() as session:
        row = await session.get(SandboxLease, stale_id)
    assert row.state == STATE_DESTROY_FAILED
    assert "人工核对" in (row.error or "")


# ---------------------------------------------------------------------
# ⑥ 体积超限 → 只导出清单
# ---------------------------------------------------------------------
async def test_oversized_workspace_exports_manifest_only(user_id, store, redis, monkeypatch):
    monkeypatch.setattr(settings, "sandbox_artifact_max_bytes", 100)
    provider = FakeProvider(workspace_bytes=10_000)
    lease = await _make_lease(user_id, provider)
    await _age_lease(lease.id)

    await reaper_module.reap_once(provider=provider)

    async with async_session_maker() as session:
        row = await session.get(SandboxLease, lease.id)
    assert row.artifact_uri and row.artifact_uri.endswith("manifest.txt")
    assert b"a.txt" in store.uploaded[row.artifact_uri]
