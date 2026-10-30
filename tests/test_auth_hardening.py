"""登录/注册路径的加固锁（审计 `01-auth` 第 1/3/4/5 项）。

四条性质互相独立，失效时都不会让任何既有用例变红：

1. **Argon2 不在事件循环线程上跑**（登录校验、登录的假哈希、注册哈希三条路径）。断言用
   线程 id 而不是耗时：耗时在 CI 上不稳定，而「跑在哪个线程」是被测代码的直接后果。
2. **rehash-on-login 仍然落库**：这段主体是从框架复制过来的，升级后最容易悄悄丢的就是它。
3. **账号维度的失败上限**：IP 桶换个出口即绕过，分布式凭据填充只能按 `user.id` 拦；
   而正确密码永远能登录——锁住的只是「继续猜」。
4. **登录失败留痕**：日志只写邮箱摘要（不落明文），指标按 `reason` 分桶。
"""

from __future__ import annotations

import hashlib
import logging
import threading
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi_users.exceptions import InvalidPasswordException, UserAlreadyExists

from app.api.rate_limit import RedisSlidingWindowLimiter
from app.config import settings
from app.schemas.user import UserCreate
from app.services.auth import (
    AUTH_LOGIN_FAILURES_TOTAL,
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    UserManager,
    get_account_failure_limiter,
    reset_account_failure_limiter,
)
from app.services.metrics import metrics


class _FakeUser:
    def __init__(self, *, email: str, hashed_password: str) -> None:
        self.id = uuid.uuid4()
        self.email = email
        self.hashed_password = hashed_password
        self.role = "user"


class _FakeUserDB:
    """只实现 `authenticate` / `create` 用到的三个方法。"""

    def __init__(self, users: dict[str, _FakeUser] | None = None) -> None:
        self.users = users or {}
        self.updates: list[tuple[object, dict]] = []
        self.created: list[dict] = []

    async def get_by_email(self, email: str):  # noqa: ANN201
        # 适配器的约定：找不到就返回 None（抛 `UserNotExists` 是 `UserManager.get_by_email` 的事）
        return self.users.get(email.lower())

    async def update(self, user, update_dict):  # noqa: ANN001, ANN201
        self.updates.append((user, update_dict))
        return user

    async def create(self, user_dict: dict):  # noqa: ANN201
        self.created.append(user_dict)
        return SimpleNamespace(
            id=uuid.uuid4(),
            role="user",
            email=user_dict["email"],
            hashed_password=user_dict["hashed_password"],
        )


class _FakePasswordHelper:
    """记录每次调用发生在哪个线程；返回值可控，避免真跑 Argon2（65ms × 用例数）。"""

    def __init__(self, *, valid: bool = False, rehash: str | None = None) -> None:
        self.valid = valid
        self.rehash = rehash
        self.verify_threads: list[int] = []
        self.hash_threads: list[int] = []

    def hash(self, password: str) -> str:
        self.hash_threads.append(threading.get_ident())
        return f"hashed::{password}"

    def verify_and_update(self, plain_password: str, hashed_password: str):  # noqa: ANN201, ARG002
        self.verify_threads.append(threading.get_ident())
        return self.valid, self.rehash


def _manager(*, valid: bool = False, rehash: str | None = None, user: _FakeUser | None = None):  # noqa: ANN202
    user = user or _FakeUser(email="victim@example.com", hashed_password="$argon2$fake")
    helper = _FakePasswordHelper(valid=valid, rehash=rehash)
    return UserManager(_FakeUserDB({user.email: user}), helper), helper


def _credentials(email: str = "victim@example.com", password: str = "wrong-password"):  # noqa: ANN202
    return SimpleNamespace(username=email, password=password)


# ---------------------------------------------------------------------
# 1. 哈希必须离开事件循环
# ---------------------------------------------------------------------
async def test_authenticate_verifies_password_in_a_worker_thread():
    """一次 Argon2 约 65ms 且占几十 MiB，跑在事件循环线程上等于让匿名请求卡住全站。"""
    manager, helper = _manager()

    result = await manager.authenticate(_credentials())

    assert result is None
    assert helper.verify_threads, "没有调用密码校验"
    assert helper.verify_threads[0] != threading.get_ident()


async def test_authenticate_unknown_email_hashes_in_a_worker_thread():
    """「邮箱不存在」时的假哈希同样不能在事件循环线程上跑，否则那条时间旁路缓解等于没做。"""
    helper = _FakePasswordHelper()
    manager = UserManager(_FakeUserDB(), helper)

    assert await manager.authenticate(_credentials(email="nobody@example.com")) is None

    assert helper.hash_threads, "没有跑假哈希"
    assert helper.hash_threads[0] != threading.get_ident()


async def test_create_hashes_password_in_a_worker_thread():
    manager, helper = _manager()

    created = await manager.create(UserCreate(email="new@example.com", password="a" * MIN_PASSWORD_LENGTH), safe=True)

    assert helper.hash_threads, "没有调用密码哈希"
    assert helper.hash_threads[0] != threading.get_ident()
    # 明文密码只在内存里过一遍，落库的是哈希
    assert "password" not in manager.user_db.created[0]
    assert created.hashed_password == f"hashed::{'a' * MIN_PASSWORD_LENGTH}"


# ---------------------------------------------------------------------
# 2. 复制的框架主体：rehash-on-login 不能丢
# ---------------------------------------------------------------------
async def test_authenticate_persists_rehashed_password():
    """上游生成的更强哈希必须落库；且直接写 `user_db`（走 `update` 会连带删掉该用户全部会话）。"""
    manager, _ = _manager(valid=True, rehash="$argon2$rehashed")

    user = await manager.authenticate(_credentials(password="right-password"))

    assert user is not None
    assert manager.user_db.updates == [(user, {"hashed_password": "$argon2$rehashed"})]


async def test_authenticate_does_not_touch_db_when_hash_is_current():
    manager, _ = _manager(valid=True, rehash=None)

    assert await manager.authenticate(_credentials(password="right-password")) is not None
    assert manager.user_db.updates == []


async def test_create_rejects_duplicate_email_before_hashing(monkeypatch):
    """查重必须发生在哈希之前：已存在的邮箱不该再白付一次 Argon2（注册路由自己补那一次）。"""
    user = _FakeUser(email="taken@example.com", hashed_password="$argon2$fake")
    manager, helper = _manager(user=user)

    with pytest.raises(UserAlreadyExists):
        await manager.create(UserCreate(email=user.email, password="a" * MIN_PASSWORD_LENGTH), safe=True)

    assert helper.hash_threads == []


# ---------------------------------------------------------------------
# 3. 账号维度的失败上限（ASVS 2.2.1）
# ---------------------------------------------------------------------
@pytest.fixture
def _account_bucket(monkeypatch):
    """每个用例用一份干净的进程内计数桶：Redis 后端下键会跨用例残留，配额也不好压到 2。"""
    monkeypatch.setattr(settings, "rate_limit_backend", "memory")
    monkeypatch.setattr(settings, "account_login_failure_limit_per_hour", 2)
    reset_account_failure_limiter()
    yield
    reset_account_failure_limiter()


async def test_account_failures_beyond_limit_return_429(_account_bucket):
    """按 IP 的桶换 IP 就绕过：单账号连续失败必须自己封顶，且 429 带 Retry-After。"""
    manager, _ = _manager()

    assert await manager.authenticate(_credentials()) is None
    assert await manager.authenticate(_credentials()) is None
    with pytest.raises(HTTPException) as excinfo:
        await manager.authenticate(_credentials())

    assert excinfo.value.status_code == 429
    assert excinfo.value.headers and "Retry-After" in excinfo.value.headers


async def test_account_limit_does_not_leak_across_accounts(_account_bucket):
    """计数按 `user.id` 分桶：打满 A 的配额不能让 B 也登不上（否则就是可远程触发的账号锁死）。"""
    manager, _ = _manager()
    other = _FakeUser(email="other@example.com", hashed_password="$argon2$fake")
    manager.user_db.users[other.email] = other

    for _ in range(2):
        await manager.authenticate(_credentials())
    with pytest.raises(HTTPException):
        await manager.authenticate(_credentials())

    assert await manager.authenticate(_credentials(email=other.email)) is None


async def test_account_limit_never_blocks_a_correct_password(_account_bucket):
    """只在失败分支计数：被「锁」的账号用正确密码依然能登录，合法用户不会被挡在门外。"""
    manager, helper = _manager()
    assert await manager.authenticate(_credentials()) is None
    assert await manager.authenticate(_credentials()) is None
    with pytest.raises(HTTPException):
        await manager.authenticate(_credentials())

    helper.valid = True
    user = await manager.authenticate(_credentials(password="right-password"))

    assert user is not None


async def test_account_limit_uses_the_shared_redis_bucket(monkeypatch):
    """生产默认 `RATE_LIMIT_BACKEND=redis`：配额必须真的落到共享存储上（进程内计数在
    多 worker 下会乘以实例数），且窗口是一小时而不是 IP 桶那个 60 秒。
    """
    monkeypatch.setattr(settings, "rate_limit_backend", "redis")
    monkeypatch.setattr(settings, "account_login_failure_limit_per_hour", 1)
    reset_account_failure_limiter()
    manager, _ = _manager()
    victim = _FakeUser(email="redis-bucket@example.com", hashed_password="$argon2$fake")
    manager.user_db.users[victim.email] = victim
    try:
        assert isinstance(get_account_failure_limiter(), RedisSlidingWindowLimiter)

        assert await manager.authenticate(_credentials(email=victim.email)) is None
        with pytest.raises(HTTPException) as excinfo:
            await manager.authenticate(_credentials(email=victim.email))

        assert excinfo.value.status_code == 429
        assert 60 < int(excinfo.value.headers["Retry-After"]) <= 3600
    finally:
        reset_account_failure_limiter()


async def test_account_limit_can_be_disabled(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_backend", "memory")
    monkeypatch.setattr(settings, "account_login_failure_limit_per_hour", 0)
    reset_account_failure_limiter()
    manager, _ = _manager()
    try:
        for _ in range(10):
            assert await manager.authenticate(_credentials()) is None
    finally:
        reset_account_failure_limiter()


# ---------------------------------------------------------------------
# 4. 登录失败留痕
# ---------------------------------------------------------------------
async def test_login_failure_logs_digest_not_plaintext(caplog):
    """日志会被轮转/抓取/贴进工单：明文邮箱等于把用户清单抄一份出去，摘要足够串起同一账号。"""
    manager, _ = _manager()
    digest = hashlib.sha256(b"victim@example.com").hexdigest()[:8]

    with caplog.at_level(logging.WARNING, logger="app.services.auth"):
        await manager.authenticate(_credentials())

    messages = [record.getMessage() for record in caplog.records]
    assert any(f"email_hash={digest}" in message and "reason=wrong_password" in message for message in messages)
    assert not any("victim@example.com" in message for message in messages)


async def test_login_failure_is_counted_with_reason_label():
    manager, _ = _manager()
    before = metrics.counter_value(AUTH_LOGIN_FAILURES_TOTAL, reason="wrong_password")

    await manager.authenticate(_credentials())

    assert metrics.counter_value(AUTH_LOGIN_FAILURES_TOTAL, reason="wrong_password") == before + 1


async def test_unknown_email_failure_is_a_separate_reason():
    """`unknown_email` 与 `wrong_password` 必须分开：前者是枚举探测，后者是猜密码。"""
    helper = _FakePasswordHelper()
    manager = UserManager(_FakeUserDB(), helper)
    before = metrics.counter_value(AUTH_LOGIN_FAILURES_TOTAL, reason="unknown_email")

    await manager.authenticate(_credentials(email="nobody@example.com"))

    assert metrics.counter_value(AUTH_LOGIN_FAILURES_TOTAL, reason="unknown_email") == before + 1


# ---------------------------------------------------------------------
# 5. 密码长度上下限（ASVS 2.1.1 / 2.1.2）
# ---------------------------------------------------------------------
def test_password_length_bounds_follow_asvs():
    """12 是 ASVS 2.1.1 的下限；128 是 2.1.2 允许的上限（改密入参也是 128，两侧必须一致）。"""
    assert MIN_PASSWORD_LENGTH == 12
    assert MAX_PASSWORD_LENGTH == 128


@pytest.mark.parametrize(
    ("password", "expected_reason"),
    [
        ("a" * (MIN_PASSWORD_LENGTH - 1), f"密码至少需要 {MIN_PASSWORD_LENGTH} 个字符"),
        ("a" * (MAX_PASSWORD_LENGTH + 1), f"密码最多 {MAX_PASSWORD_LENGTH} 个字符"),
    ],
)
async def test_validate_password_enforces_length_bounds(password, expected_reason):
    manager, _ = _manager()

    with pytest.raises(InvalidPasswordException) as excinfo:
        await manager.validate_password(password, user=None)

    assert excinfo.value.reason == expected_reason


@pytest.mark.parametrize("length", [MIN_PASSWORD_LENGTH, MAX_PASSWORD_LENGTH])
async def test_validate_password_accepts_boundary_lengths(length):
    manager, _ = _manager()

    await manager.validate_password("a" * length, user=None)
