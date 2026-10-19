"""启动期 `role` 列迁移的行为锁（`app/db.py::_migrate_user_role_column`）。

**为什么单独一个文件**：这段代码是启动时无人值守执行的，它会**改写权限列**。
它出错的方式不是抛异常，而是「跑完了、什么都没报、某个账号的权限变了」——
只能在单元层面把每一步的结果钉住。

历史缺陷（本次修复的起因）：
    回填（`is_superuser=1 AND role='user'` → `superadmin`）排在规范化**之前**，
    而规范化用精确字符串比较。手写 `role='ADMIN', is_superuser=1` 的行：
      第 1 次启动 → 回填不匹配 → 规范化成 `'user'`，但 `is_superuser` 仍是 1；
      第 2 次启动 → 回填命中 → `superadmin`。
    即「运维想给一个 admin，两次重启后拿到最高权限」。而 `app/roles.py::Role.parse`
    对同一个值（`strip().lower()` 后比较）一直认它是 admin——两处语义相反。

下面每个用例都用一个独立的临时 SQLite 文件（不是测试会话共用的那个库）：
迁移直接建表/改表，共用库会让别的用例莫名其妙地少一列或多一列。
"""

import uuid

from sqlalchemy.ext.asyncio import create_async_engine

from app.db import _migrate_user_role_column
from app.roles import ROLE_ADMIN, ROLE_SUPERADMIN, ROLE_USER


async def _make_legacy_db(tmp_path, rows):
    """造一个「role 列出现之前」的库：只有 is_superuser，没有 role。

    rows 是 `(email, is_superuser)` 的列表。返回一个连到该文件的 engine。
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    async with engine.begin() as conn:
        # 只要迁移关心的两列：迁移自己用 PRAGMA 判断表存不存在，其余列无关
        await conn.exec_driver_sql(
            "CREATE TABLE user (id CHAR(32) PRIMARY KEY, email VARCHAR(320), is_superuser BOOLEAN DEFAULT 0)"
        )
        for email, is_superuser in rows:
            await conn.exec_driver_sql(
                "INSERT INTO user (id, email, is_superuser) VALUES (?, ?, ?)",
                (uuid.uuid4().hex, email, 1 if is_superuser else 0),
            )
    return engine


async def _run_migration(engine):
    async with engine.begin() as conn:
        await _migrate_user_role_column(conn)


async def _roles(engine):
    """读出 `{email: (role, is_superuser)}`，直接看迁移后的库。"""
    async with engine.connect() as conn:
        result = await conn.exec_driver_sql("SELECT email, role, is_superuser FROM user ORDER BY email")
        return {row[0]: (row[1], bool(row[2])) for row in result.fetchall()}


async def test_legacy_superuser_is_backfilled(tmp_path):
    """角色列出现之前的历史超管必须拿到 superadmin，否则升级瞬间失去入口。"""
    engine = await _make_legacy_db(tmp_path, [("old-admin@example.com", True), ("plain@example.com", False)])
    try:
        await _run_migration(engine)
        assert await _roles(engine) == {
            "old-admin@example.com": (ROLE_SUPERADMIN, True),
            "plain@example.com": (ROLE_USER, False),
        }
    finally:
        await engine.dispose()


async def test_handwritten_uppercase_admin_is_not_demoted(tmp_path):
    """手工写成 'ADMIN' 必须是 admin —— 与 `Role.parse` 的 `strip().lower()` 口径一致。

    这条在修复前会得到 `'user'`（被当非法值降级），一次重启就够。
    """
    engine = await _make_legacy_db(tmp_path, [("handwritten@example.com", False)])
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("ALTER TABLE user ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'user'")
            await conn.exec_driver_sql("UPDATE user SET role = 'ADMIN'")
        await _run_migration(engine)
        assert await _roles(engine) == {"handwritten@example.com": (ROLE_ADMIN, False)}
    finally:
        await engine.dispose()


async def test_harmless_whitespace_is_normalized(tmp_path):
    """`' admin '` 前后空白不能让它变成普通用户。"""
    engine = await _make_legacy_db(tmp_path, [("spaced@example.com", False)])
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("ALTER TABLE user ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'user'")
            await conn.exec_driver_sql("UPDATE user SET role = ' superadmin '")
        await _run_migration(engine)
        assert await _roles(engine) == {"spaced@example.com": (ROLE_SUPERADMIN, False)}
    finally:
        await engine.dispose()


async def test_invalid_role_with_stale_superuser_flag_stays_demoted(tmp_path):
    """认不出的 role **不**能靠残留的 is_superuser 位爬回 superadmin。

    修复前的实际行为：第 1 次启动降成 'user' 但保留 is_superuser=1，
    第 2 次启动被回填成 superadmin。这里连跑两次，断言两次结果相同。
    """
    engine = await _make_legacy_db(tmp_path, [("typo@example.com", True)])
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("ALTER TABLE user ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'user'")
            await conn.exec_driver_sql("UPDATE user SET role = 'admn'")
        await _run_migration(engine)
        after_first = await _roles(engine)
        await _run_migration(engine)
        after_second = await _roles(engine)

        # 失败方向必须是降权：写错的值 + 残留超管位 ≠ 超管
        assert after_first == {"typo@example.com": (ROLE_USER, False)}
        # 幂等：第二次启动不能改变第一次的结论
        assert after_second == after_first
    finally:
        await engine.dispose()


async def test_migration_is_idempotent_on_a_clean_db(tmp_path):
    """连跑三次，正常账号一个字节都不变。"""
    engine = await _make_legacy_db(tmp_path, [("u@example.com", False), ("a@example.com", True)])
    try:
        await _run_migration(engine)
        first = await _roles(engine)
        for _ in range(2):
            await _run_migration(engine)
        assert await _roles(engine) == first
    finally:
        await engine.dispose()


async def test_missing_table_is_a_noop(tmp_path):
    """表还不存在时不能抛——全新库由 `create_all` 带上 role，这里只该直接返回。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}")
    try:
        await _run_migration(engine)  # 不建任何表
    finally:
        await engine.dispose()
