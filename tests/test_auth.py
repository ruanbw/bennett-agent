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
