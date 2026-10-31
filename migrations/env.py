"""Alembic 迁移环境：异步引擎（asyncpg）+ `asyncio.run` 包 `run_migrations`（官方 async 模板写法）。

连接串来自 `app.config.settings.database_url`（alembic.ini 里不落连接串）；target_metadata 是
`app.db` 的 `Base.metadata`（导入 `app.db` 会把全部模型模块带上，metadata 才是完整的）。

注意：本仓库的策略是「sql/init.sql 为表结构唯一事实来源」，target_metadata 只供 autogenerate
比对参考；增量迁移手写，禁止用 `--autogenerate` 的产出直接提交（见 README「增量迁移」）。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import settings
from app.db import Base

# Alembic Config 对象：读取 alembic.ini 的 [alembic] 段。
config = context.config

# CLI 的日志配置只在有 ini 文件时生效（编程式调用可能没有）。
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 连接串：与应用同一份配置（settings 读 .env / 环境变量）。
config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：不连库，只产出 SQL 脚本（`alembic upgrade head --sql`）。"""

    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """异步引擎上执行迁移。NullPool：短命进程不需要池，也不用回收。"""

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
