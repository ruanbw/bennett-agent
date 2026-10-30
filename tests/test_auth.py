"""会话生命周期参数的守卫（`app/services/auth.py`、`app/db.py`）。

HTTP 认证链路用例（注册/登录/cookie/登出/会话失效）已随全量/端到端层移除；这里保留
两条纯逻辑守卫：`lifetime_seconds<=0` 必须拒绝——框架用真值判断过期，0 会让会话
**永不过期**。
"""

import pytest

from app.config import settings
from app.db import cleanup_expired_access_tokens
from app.services.auth import get_database_strategy


@pytest.mark.parametrize("lifetime", [0, -1, -3600])
def test_get_database_strategy_rejects_non_positive_lifetime(monkeypatch, lifetime):
    """`lifetime_seconds<=0` 必须拒绝：框架用真值判断过期，0 会让会话**永不过期**。"""
    monkeypatch.setattr(settings, "session_lifetime_seconds", lifetime)

    with pytest.raises(ValueError, match="永不过期"):
        # 校验发生在使用适配器之前，所以这里不需要真数据库
        get_database_strategy(None)


@pytest.mark.parametrize("lifetime", [0, -1])
async def test_cleanup_rejects_non_positive_lifetime(lifetime):
    """清理函数同样拒绝非正值，否则 cutoff=now 会把全部有效会话删光。"""
    with pytest.raises(ValueError, match="永不过期"):
        await cleanup_expired_access_tokens(lifetime)


async def test_unknown_email_still_runs_a_password_hash():
    """登录耗时不能区分「邮箱不存在」与「密码错误」（I-12）。

    fastapi-users 的 `authenticate` 在用户不存在时会故意跑一次 hash；这条用例把它
    锁住，避免升级框架后静默丢掉该缓解。同时断言这次假哈希必须发生在**另一个线程**：
    我们覆盖了 `authenticate`，缓解留在事件循环线程上就等于没做（一次 Argon2 约 65ms）。
    """
    import threading
    from types import SimpleNamespace

    from fastapi_users.exceptions import UserNotExists

    from app.services.auth import UserManager

    class _MissingUserDB:
        async def get_by_email(self, email):  # noqa: ANN001, ANN202
            raise UserNotExists()

    class _CountingHelper:
        def __init__(self) -> None:
            self.hashed = 0
            self.thread_ids: list[int] = []

        def hash(self, password):  # noqa: ANN001, ANN202
            self.hashed += 1
            self.thread_ids.append(threading.get_ident())
            return "hashed"

    helper = _CountingHelper()
    manager = UserManager(_MissingUserDB(), helper)
    main_thread = threading.get_ident()

    result = await manager.authenticate(SimpleNamespace(username="nobody@example.com", password="whatever123"))

    assert result is None
    assert helper.hashed == 1
    assert helper.thread_ids[0] != main_thread
