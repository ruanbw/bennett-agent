"""沙箱客户端端点（`app/api/sandbox.py`）：心跳与状态。

## 为什么直接调处理函数，而不是起 TestClient

这两个端点的授权已在依赖里（`current_active_user`）完成，剩下要锁住的是**它们对租约
做了什么**：心跳只写 Redis、永不泄漏「有没有机器」；状态按 `(user_id, session_id)` 查、
查不到就 404。直接调函数能把这三条断言写得很直白，而起一个带 cookie 的客户端只会把
失败点移到登录环节。

## 最要紧的一条

心跳在「没有机器」与「机器属于别人」两种情况下必须**完全一样**（都是 204、都不写键）。
否则它就变成了存在性探测器：攻击者拿别人的 `sess_...` 试探，能反推谁正在用沙箱。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import sandbox as api
from app.config import settings
from app.db import User, async_session_maker
from app.schemas.sandbox import SandboxHeartbeatRequest
from app.services.sandbox.base import SandboxHandle, SandboxSpec
from app.services.sandbox.lease import (
    DESTROY_IDLE,
    ensure_lease,
    heartbeat_key,
    mark_destroyed,
    mark_destroying,
)


class FakeProvider:
    name = "opensandbox"

    def __init__(self) -> None:
        self.created = 0

    async def create(self, spec: SandboxSpec) -> SandboxHandle:
        self.created += 1
        return SandboxHandle(ref=f"ref-{self.created}", ready_ms=1)


class FakeRedis:
    def __init__(self) -> None:
        self.keys: dict[str, int] = {}

    async def set(self, name: str, value, *, ex=None):  # noqa: ANN001
        self.keys[name] = ex or 0
        return True


@pytest.fixture
async def user() -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@example.com",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        is_verified=True,
    )
    async with async_session_maker() as session:
        session.add(user)
        await session.commit()
    return user


@pytest.fixture
def redis(monkeypatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr(api, "get_redis", lambda: fake)
    monkeypatch.setattr(settings, "sandbox_enabled", True)
    return fake


async def _lease(user: User, session_id: str = "sess_x") -> str:
    async with async_session_maker() as session:
        lease, _ = await ensure_lease(session, user_id=user.id, session_id=session_id, create=FakeProvider().create)
    return lease.id


async def test_heartbeat_touches_redis_key(user, redis):
    lease_id = await _lease(user)

    async with async_session_maker() as session:
        response = await api.heartbeat(SandboxHeartbeatRequest(session_id="sess_x"), user=user, session=session)

    assert response.status_code == 204
    assert redis.keys[heartbeat_key(lease_id)] == settings.sandbox_idle_seconds


async def test_heartbeat_without_lease_still_returns_204_and_writes_nothing(user, redis):
    """没有机器 ≠ 报错：客户端不该需要知道「现在有没有环境」。"""
    async with async_session_maker() as session:
        response = await api.heartbeat(SandboxHeartbeatRequest(session_id="sess_none"), user=user, session=session)
    assert response.status_code == 204
    assert redis.keys == {}


async def test_heartbeat_cannot_touch_another_users_session(user, redis):
    """别人的会话标识拿到的是「没有机器」，不是「权限不足」——不给存在性探测器。"""
    other = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@example.com",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        is_verified=True,
    )
    async with async_session_maker() as session:
        session.add(other)
        await session.commit()
    victim_lease = await _lease(other, "sess_victim")

    async with async_session_maker() as session:
        response = await api.heartbeat(SandboxHeartbeatRequest(session_id="sess_victim"), user=user, session=session)

    assert response.status_code == 204
    assert heartbeat_key(victim_lease) not in redis.keys


async def test_status_reports_state_and_expiry(user, redis):
    await _lease(user)

    async with async_session_maker() as session:
        status = await api.sandbox_status(session_id="sess_x", user=user, session=session)

    assert status.state == "ready"
    assert status.expires_at is not None
    assert status.artifact_uri is None


async def test_status_keeps_reporting_after_recycle(user, redis):
    """已回收也要能查到（带原因与产物），否则用户以为东西被弄丢了。"""
    lease_id = await _lease(user)
    async with async_session_maker() as session:
        assert await mark_destroying(session, lease_id) is True
    async with async_session_maker() as session:
        await mark_destroyed(session, lease_id, reason=DESTROY_IDLE, artifact_uri="sandbox/x/y.tar.gz")

    async with async_session_maker() as session:
        status = await api.sandbox_status(session_id="sess_x", user=user, session=session)

    assert status.state == "destroyed"
    assert status.artifact_uri == "sandbox/x/y.tar.gz"


async def test_status_404_when_session_never_had_environment(user, redis):
    async with async_session_maker() as session:
        with pytest.raises(HTTPException) as excinfo:
            await api.sandbox_status(session_id="sess_none", user=user, session=session)
    assert excinfo.value.status_code == 404


async def test_endpoints_are_inert_when_sandbox_disabled(user, monkeypatch):
    monkeypatch.setattr(settings, "sandbox_enabled", False)

    async with async_session_maker() as session:
        response = await api.heartbeat(SandboxHeartbeatRequest(session_id="sess_x"), user=user, session=session)
        assert response.status_code == 204
        with pytest.raises(HTTPException) as excinfo:
            await api.sandbox_status(session_id="sess_x", user=user, session=session)
    assert excinfo.value.status_code == 404


def test_session_id_pattern_rejects_foreign_shapes():
    """/api/sandbox 的查询参数与 /api/chat 的字段用同一个形状，不能一边宽一边窄。"""
    from pydantic import ValidationError

    from app.schemas.chat import SESSION_ID_PATTERN as chat_pattern
    from app.schemas.sandbox import SESSION_ID_PATTERN as sandbox_pattern

    assert chat_pattern == sandbox_pattern
    with pytest.raises(ValidationError):
        SandboxHeartbeatRequest(session_id="../../etc/passwd")
    assert SimpleNamespace(session_id="sess_abc123").session_id  # 形状合法的样例


# ---------------------------------------------------------------------
# 产物清单
# ---------------------------------------------------------------------
class FakeObjectStore:
    """只实现产物端点用到的那两个动作。"""

    def __init__(self, objects: dict[str, int]) -> None:
        self.objects = objects

    async def stat_size(self, key: str):  # noqa: ANN202
        return self.objects.get(key)

    async def presigned_get_url(self, key: str, expires_in: int) -> str:
        return f"https://s3.example/{key}?expires={expires_in}"


@pytest.fixture
def fake_store(monkeypatch):
    """替换对象存储。

    打的是**包**（`app.services.storage`）而不是 `app.services.storage.s3`：端点内部
    是 `from app.services.storage import get_object_store`，那是包 `__init__` 里的绑定，
    只 patch 子模块不会影响它。
    """
    import app.services.storage as storage_pkg

    store = FakeObjectStore({"sandbox/u/l/workspace.tar.gz": 4096})
    monkeypatch.setattr(storage_pkg, "get_object_store", lambda: store)
    monkeypatch.setattr(settings, "sandbox_enabled", True)
    return store


async def test_artifacts_turn_the_key_into_a_presigned_link(user, redis, fake_store):
    """`artifact_uri` 是对象键，用户拿不到——这个端点必须是它的唯一出口。"""
    lease_id = await _lease(user)
    async with async_session_maker() as session:
        assert await mark_destroying(session, lease_id) is True
    async with async_session_maker() as session:
        await mark_destroyed(session, lease_id, reason=DESTROY_IDLE, artifact_uri="sandbox/u/l/workspace.tar.gz")

    async with async_session_maker() as session:
        result = await api.sandbox_artifacts(session_id="sess_x", user=user, session=session)

    assert result.lease_id == lease_id
    assert result.state == "destroyed"
    names = [item.name for item in result.artifacts]
    assert names == ["workspace.tar.gz"]
    artifact = result.artifacts[0]
    assert artifact.download_url.startswith("https://s3.example/")
    assert artifact.size_bytes == 4096
    assert artifact.expires_at is not None


async def test_artifacts_are_empty_before_the_machine_is_recycled(user, redis, fake_store):
    """机器还活着时没有产物。这不是错误——返回 200 加空数组，不是 404。"""
    await _lease(user)

    async with async_session_maker() as session:
        result = await api.sandbox_artifacts(session_id="sess_x", user=user, session=session)

    assert result.state == "ready"
    assert result.artifacts == []


async def test_artifacts_are_empty_when_no_lease_ever_existed(user, redis, fake_store):
    """从来没有过机器也没有产物，但仍然 200：前端轮询不该天天报错。"""
    async with async_session_maker() as session:
        result = await api.sandbox_artifacts(session_id="sess_none", user=user, session=session)

    assert result.lease_id is None
    assert result.state is None
    assert result.artifacts == []


async def test_artifacts_degrade_to_empty_when_storage_is_not_configured(user, redis, monkeypatch):
    """没配对象存储的部署本来就没有产物，降级成空列表而不是 503。"""
    import app.services.storage as storage_pkg
    from app.services.storage import StorageNotConfigured

    def _boom():
        raise StorageNotConfigured("未配置")

    monkeypatch.setattr(storage_pkg, "get_object_store", _boom)
    monkeypatch.setattr(settings, "sandbox_enabled", True)

    lease_id = await _lease(user)
    async with async_session_maker() as session:
        assert await mark_destroying(session, lease_id) is True
    async with async_session_maker() as session:
        await mark_destroyed(session, lease_id, reason=DESTROY_IDLE, artifact_uri="sandbox/u/l/workspace.tar.gz")

    async with async_session_maker() as session:
        result = await api.sandbox_artifacts(session_id="sess_x", user=user, session=session)

    assert result.artifacts == []


async def test_artifacts_404_when_sandbox_disabled(user, redis, monkeypatch):
    monkeypatch.setattr(settings, "sandbox_enabled", False)

    async with async_session_maker() as session:
        with pytest.raises(HTTPException) as excinfo:
            await api.sandbox_artifacts(session_id="sess_x", user=user, session=session)
    assert excinfo.value.status_code == 404
