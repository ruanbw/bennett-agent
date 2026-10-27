"""结构守门：sql/init.sql 能建出与模型一致的结构，且可重复执行。"""

from __future__ import annotations

import asyncio

from sqlalchemy import inspect

from app.db import Base, build_engine, engine, init_sql_text, verify_schema_at_head


def _expected_tables() -> set[str]:
    return set(Base.metadata.tables)


def _actual_tables(target_engine=engine) -> set[str]:
    async def run() -> set[str]:
        async with target_engine.connect() as connection:
            return set(await connection.run_sync(lambda c: inspect(c).get_table_names()))

    return asyncio.run(run())


def test_schema_is_idempotent():
    """重复执行不报错——多实例同时启动就是这样跑的。"""
    asyncio.run(verify_schema_at_head())
    asyncio.run(verify_schema_at_head())


def test_schema_covers_every_model_table():
    missing = _expected_tables() - _actual_tables()
    assert not missing, f"sql/init.sql 少了这些表：{sorted(missing)}"


def test_models_and_init_sql_do_not_drift():
    """模型加列却忘了改脚本时，这一条会红。"""

    async def run() -> list[str]:
        problems: list[str] = []
        async with engine.connect() as connection:

            def compare(sync_connection):  # noqa: ANN001, ANN202
                inspector = inspect(sync_connection)
                for table in Base.metadata.sorted_tables:
                    db_columns = {col["name"] for col in inspector.get_columns(table.name)}
                    model_columns = {col.name for col in table.columns}
                    if db_columns != model_columns:
                        problems.append(
                            f"{table.name}: 库里多 {sorted(db_columns - model_columns)}，"
                            f"模型多 {sorted(model_columns - db_columns)}"
                        )

            await connection.run_sync(compare)
        return problems

    problems = asyncio.run(run())
    assert not problems, "模型与 sql/init.sql 漂移：\n" + "\n".join(problems)


def test_schema_builds_from_scratch():
    """空库能建出完整结构（脚本不依赖任何已有对象）。"""
    import asyncpg
    from sqlalchemy.engine import make_url

    from app.config import settings

    scratch = "bennett_test_schema_check"
    url = make_url(settings.database_url)

    async def admin(sql: str) -> None:
        connection = await asyncpg.connect(
            host=url.host or "127.0.0.1",
            port=url.port or 5432,
            user=url.username,
            password=url.password,
            database="postgres",
        )
        try:
            await connection.execute(sql)
        finally:
            await connection.close()

    asyncio.run(admin(f'DROP DATABASE IF EXISTS "{scratch}"'))
    asyncio.run(admin(f'CREATE DATABASE "{scratch}"'))
    try:
        scratch_engine = build_engine(url.set(database=scratch).render_as_string(hide_password=False))

        async def create() -> None:
            async with scratch_engine.begin() as connection:
                for statement in (s.strip() for s in init_sql_text().split(";") if s.strip()):
                    await connection.exec_driver_sql(statement)
            await scratch_engine.dispose()

        asyncio.run(create())
        assert _expected_tables() <= _actual_tables(scratch_engine)
    finally:
        asyncio.run(admin(f'DROP DATABASE IF EXISTS "{scratch}"'))
