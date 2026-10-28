"""测试全局配置：真实 Postgres + 真实 Redis，测试库与开发库物理隔离。

用真库而不是 SQLite：外键是否强制、`FOR UPDATE SKIP LOCKED` 存不存在、`JSONB` 的行为
都是被测逻辑的一部分，换成另一套语义的库，用例会全绿而生产上的锁与约束一次都没跑到。

测试基座是：

    Postgres   `bennett_test` 库（可用 TEST_DATABASE_URL 覆盖）
    Redis      第 15 号库（`REDIS_URL` 默认指向它，键前缀带 `bennett-test`）
    RustFS     `bennett-test-artifacts` 桶

后两者由 `docker compose up -d postgres redis rustfs` 提供。**没有它们时测试会失败**，
而不是静默跳过：一个「悄悄跳过集成测试」的套件会让人误以为全绿，而 CI 与本地跑的
根本不是同一件事。

## 为什么在导入 app 之前设置环境变量

`app/db.py` 在导入时就按 `settings.database_url` 构造 engine，`app/config/settings.py`
在导入时就实例化 `Settings`。所有重定向必须在**任何 app 模块被导入之前**完成，因此
写在 conftest 的模块级（pytest 保证 conftest 先于测试模块导入），而不是 fixture 里。

## 结构怎么来：执行真实的 sql/init.sql

**不用 `metadata.create_all`**：二者会分叉（server_default、索引名、列序），而分叉的
代价是「脚本里写错的 DDL 在测试里永远不会被发现」。

每个用例开始前 TRUNCATE 所有表，因此用例之间互不影响，也不需要在用例里自己清表。
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: 测试库连接串。默认值对应 docker-compose 里 postgres 服务的本地端口。
#: CI 与需要并行跑多套测试时用 TEST_DATABASE_URL 覆盖。
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://bennett:bennett@127.0.0.1:5432/bennett_test",
)

os.environ["DATABASE_URL"] = TEST_DATABASE_URL

#: Redis 用第 15 号库并加独立前缀：与开发库（0 号）物理分开，
#: 即使有人在本机同时跑服务和测试，也不会互相清掉限流计数与会话缓存。
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/15")
os.environ.setdefault("REDIS_KEY_PREFIX", "bennett-test")

#: 测试用自己的桶：上传用例会删对象，落到开发桶里就是真实数据丢失。
os.environ.setdefault("S3_BUCKET", "bennett-test-artifacts")
os.environ.setdefault("S3_PREFIX", "test-artifacts")

#: APP_ENV=test 让 .env.development 的覆盖失效（.env.test 不存在时其余配置来自 .env）。
#: 取值合法性由 settings 的白名单校验（development/production/staging/test）。
os.environ.setdefault("APP_ENV", "test")

#: 测试里用 NullPool：部分用例用同步 `TestClient`，它在另一个线程的事件循环里跑请求，
#: 而池里的 asyncpg 连接绑定在会话循环上——复用会报「Future attached to a different loop」。
os.environ.setdefault("DATABASE_POOL_MODE", "null")

#: 关掉上传解析的自动轮询这类后台行为？没有——worker 是显式调用的函数，
#: 不会在测试里自启动。这里只把轮询间隔调到很小，避免等待型用例真的睡 2 秒。
os.environ.setdefault("UPLOAD_WORKER_POLL_SECONDS", "0.05")

#: 沙箱默认关：**测试结果不能取决于开发者本地 `.env` 开了什么**。
#: 之前 `SANDBOX_ENABLED=true` 写在 `.env` 里时，能力探针的「工具清单必须精确相等」
#: 那条用例会多出四个沙箱工具而失败——失败出现在别人的机器上，而在 CI 上又看不到。
#: 需要沙箱的用例显式 `monkeypatch.setattr(settings, "sandbox_enabled", True)`。
os.environ.setdefault("SANDBOX_ENABLED", "false")

_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")


async def _ensure_database_exists() -> None:
    """确保测试库存在（幂等）。

    `docker compose up` 只建 `POSTGRES_DB`（bennett），不会建测试库。让测试自己建，
    是为了避免「先手工执行一条 psql」这种只在某台机器上成立的隐性前置条件。
    """
    import asyncpg
    from sqlalchemy.engine import make_url

    url = make_url(TEST_DATABASE_URL)
    database = url.database or "bennett_test"
    if not _SAFE_IDENTIFIER.match(database):
        raise pytest.UsageError(f"TEST_DATABASE_URL 里的库名不合法：{database!r}")

    connection = await asyncpg.connect(
        host=url.host or "127.0.0.1",
        port=url.port or 5432,
        user=url.username,
        password=url.password,
        database="postgres",
    )
    try:
        exists = await connection.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", database)
        if not exists:
            # 库名已通过白名单校验，不可能是注入面
            await connection.execute(f'CREATE DATABASE "{database}"')
    finally:
        await connection.close()


def _upgrade_to_head() -> None:
    """在测试库上执行 sql/init.sql（幂等，可重复调用）。"""
    from app.db import engine, verify_schema_at_head

    asyncio.run(verify_schema_at_head())
    asyncio.run(engine.dispose())


@pytest.fixture(scope="session", autouse=True)
def _prepare_test_database() -> None:
    """会话级：建库（如缺失）→ 建表 → 清空 Redis 测试库。

    这三件事都必须是**会话级**：每个用例重跑一遍建表会让套件慢到没人愿意跑，
    而 Redis 不先清空会让上一次运行残留的键影响限流用例（窗口计数在 60 秒内有效）。
    """
    asyncio.run(_ensure_database_exists())
    _upgrade_to_head()

    async def flush_redis() -> None:
        # 这里**不用** `get_redis()`：它是进程级单例，而 `asyncio.run` 会开一个用完就关的
        # 临时事件循环。把单例建在那上面，后续用例拿到的就是「绑定在已关闭循环上的客户端」，
        # 报错发生在第一次 Redis 调用处，看起来跟 Redis 本身完全无关。
        from redis.asyncio import Redis

        client = Redis.from_url(os.environ["REDIS_URL"], decode_responses=False)
        try:
            await client.flushdb()
        finally:
            await client.aclose()

    asyncio.run(flush_redis())
    yield


@pytest.fixture(autouse=True)
async def _reset_state_between_tests():
    """每个用例前清空业务表与进程内缓存。

    用 TRUNCATE 而不是逐个 DELETE：
    - 一条语句清掉所有表，`CASCADE` 自动处理外键顺序，不需要维护删除顺序；
    - `RESTART IDENTITY` 让自增序列归零（本项目的序列只用于 sync 版本号，但仍要确定）；
    - 只列 `Base.metadata` 里的表，库里没有别的业务表。

    为什么是「之前」而不是「之后」：用例失败时残留数据会让后续用例莫名其妙地红，
    而在失败现场再清一次会把报错堆栈弄乱。之前清一次就够。
    """
    from sqlalchemy import text

    from app.db import Base, engine

    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))

    # 进程内状态：限流告警节流、搜索缓存 LRU。它们不随数据库清空而复位。
    from app.services.redis_client import reset_state_for_tests
    from app.tools.tavily_cache import reset_search_cache_memory_only

    reset_state_for_tests()
    reset_search_cache_memory_only()
    yield


@pytest.fixture(scope="session", autouse=True)
async def _dispose_engine_at_end():
    """会话结束释放连接池，避免 pytest 退出时挂着一堆 Postgres 连接。

    必须是**异步**的会话级 fixture：`asyncio.run` 会开一个新循环，而连接池里的连接
    属于会话循环，跨循环 `dispose()` 会抛 `Future attached to a different loop`。
    """
    yield

    from app.db import engine
    from app.services.redis_client import close_redis

    await close_redis()
    await engine.dispose()
