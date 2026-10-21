"""SQLite 连接参数（`app/db.py` 的 `apply_sqlite_pragmas` / `check_journal_mode`）。

这些 PRAGMA 是**每连接**生效的运行时设置，出错方式很安静：

- 不开 `foreign_keys`：级联删除失效，删用户后会话行残留（单元测试因为没开 PRAGMA
  反而全绿）；
- `journal_mode=WAL` 在 NFS/不支持的卷上会**静默退回** `delete`，一切照常工作，只是
  读写开始互相阻塞；
- `synchronous` 保持默认 FULL 时每次提交都要 fsync，流式落库被磁盘拖慢；设成 OFF 则
  真的可能坏库。
"""

import logging
import sqlite3

from sqlalchemy.ext.asyncio import create_async_engine

from app.db import apply_sqlite_pragmas, check_journal_mode


def _pragma(connection: sqlite3.Connection, name: str) -> object:
    row = connection.execute(f"PRAGMA {name}").fetchone()
    return row[0] if row else None


def test_pragmas_are_applied_to_a_bare_connection(tmp_path):
    """裸 sqlite3 连接上逐个验证取值——不去动全局 engine。"""
    connection = sqlite3.connect(tmp_path / "pragmas.db")
    try:
        apply_sqlite_pragmas(connection)

        assert _pragma(connection, "foreign_keys") == 1
        assert str(_pragma(connection, "journal_mode")).lower() == "wal"
        # 1 = NORMAL。WAL 下的推荐搭配：只在检查点 fsync，掉电最多丢最后几个事务
        assert _pragma(connection, "synchronous") == 1
        assert _pragma(connection, "busy_timeout") == 5000
    finally:
        connection.close()


def test_pragmas_are_idempotent(tmp_path):
    connection = sqlite3.connect(tmp_path / "pragmas.db")
    try:
        apply_sqlite_pragmas(connection)
        apply_sqlite_pragmas(connection)  # 连接池复用时会重复设置

        assert str(_pragma(connection, "journal_mode")).lower() == "wal"
    finally:
        connection.close()


async def test_check_journal_mode_reports_the_actual_mode(tmp_path):
    """返回值来自库本身，不是我们「设置过什么」——静默退回 delete 时才能报出来。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mode.db'}")
    try:
        async with engine.connect() as conn:
            # 新库默认是 delete（未设 WAL 的连接不会继承别人的设置）
            await conn.exec_driver_sql("PRAGMA journal_mode=delete")
            assert await check_journal_mode(conn) == "delete"

            await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            assert await check_journal_mode(conn) == "wal"
    finally:
        await engine.dispose()


async def test_check_journal_mode_never_raises(caplog):
    """诊断信息不该让启动失败：查不出来就返回空串并留日志。"""

    class BrokenConnection:
        async def exec_driver_sql(self, _sql: str):  # noqa: ANN202
            raise sqlite3.OperationalError("database is locked")

    with caplog.at_level(logging.WARNING, logger="app.db"):
        assert await check_journal_mode(BrokenConnection()) == ""

    assert any("journal_mode" in record.getMessage() for record in caplog.records)
