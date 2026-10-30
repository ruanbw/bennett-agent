"""测试全局配置：功能测试，零基础设施。

用例直接调用函数 / 服务 / 路由处理函数，协作者传替身。Redis 用进程内 fakeredis，
不连数据库。环境变量与 fakeredis 注入必须在导入 app 之前完成（Settings 与 engine
在导入时构造），所以写在 conftest 模块级。
"""

from __future__ import annotations

import os
from pathlib import Path

import fakeredis
import fakeredis.aioredis
import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: pytest-xdist worker 名（串行为 "main"）
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")
_RESOURCE_SUFFIX = "" if _WORKER == "main" else f"_{_WORKER}"

os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/15")
os.environ.setdefault("REDIS_KEY_PREFIX", f"bennett-test{_RESOURCE_SUFFIX}")
os.environ.setdefault("APP_ENV", "test")
#: 搜索缓存用进程内 LRU，不用 Redis 后端
os.environ.setdefault("SEARCH_CACHE_BACKEND", "memory")
os.environ.setdefault("SANDBOX_ENABLED", "false")

from app.services import redis_client as _redis_client  # noqa: E402

#: 进程内 Redis：限流 Lua 脚本 / 会话缓存 / 搜索缓存都落到这里，不碰真实端口
_fake_redis_server = fakeredis.FakeServer()
_fake_redis_client = fakeredis.aioredis.FakeRedis(server=_fake_redis_server, decode_responses=False)
_redis_client._client = _fake_redis_client


async def _close_redis_for_tests() -> None:
    """空操作：close_redis() 会把 _client 置 None，下一次 get_redis() 就会去连真实 Redis。"""


_redis_client.close_redis = _close_redis_for_tests  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _reset_state_between_tests():
    """复位进程级单例状态（限流告警节流、搜索缓存 LRU）。"""
    from app.services.redis_client import reset_state_for_tests
    from app.tools.tavily_cache import reset_search_cache_memory_only

    reset_state_for_tests()
    reset_search_cache_memory_only()
    yield
