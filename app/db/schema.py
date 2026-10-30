"""运行时 schema 守卫：执行 sql/init.sql，并逐列/逐索引比对模型与库，漂移即拒绝启动。"""

import logging

from sqlalchemy import UniqueConstraint, text
from sqlalchemy.dialects import postgresql

from app.config import settings
from app.db.base import PROJECT_ROOT, Base, engine

logger = logging.getLogger(__name__)

#: schema 唯一事实来源：整段幂等，可直接重复执行。
INIT_SQL_PATH = PROJECT_ROOT / "sql" / "init.sql"


def init_sql_text() -> str:
    return INIT_SQL_PATH.read_text(encoding="utf-8")


async def verify_schema_at_head() -> None:
    """执行 sql/init.sql 建表，然后逐列/逐索引比对模型与库，漂移就拒绝启动。

    `CREATE TABLE IF NOT EXISTS` 对既有表是 no-op，新列永远建不出来，而启动照样成功，直到某条
    冷门路径 SELECT 才 500（CI 是空库，测试抓不到）；故比对后给出可复制的 ALTER TABLE，宁可起不来。
    """
    if not settings.database_apply_schema:
        logger.warning("已跳过建库脚本（DATABASE_APPLY_SCHEMA=false）")
        return
    # SQLAlchemy 的 text() 走预处理语句，一次只能一条命令；这里逐句执行。
    statements = [s.strip() for s in init_sql_text().split(";") if s.strip()]
    async with engine.begin() as connection:
        for statement in statements:
            await connection.execute(text(statement))

    missing = await _missing_columns()
    if missing:
        detail = "\n".join(f"  ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column};" for table, column in missing)
        raise RuntimeError(
            "数据库结构已漂移：模型里有这些列，库里没有——`sql/init.sql` 的 "
            "CREATE TABLE IF NOT EXISTS 对既有表不会加列。请重建开发库"
            "（docker compose down -v && docker compose up -d postgres），"
            "或在生产上手工执行：\n" + detail
        )

    missing_indexes = await _missing_indexes()
    if missing_indexes:
        detail = "\n".join(f"  {create_sql}" for _, create_sql in missing_indexes)
        raise RuntimeError(
            "数据库结构已漂移：模型里有这些索引/唯一约束，库里没有——索引级别的 IF NOT EXISTS 是"
            "真会补建的，所以走到这里通常意味着 `sql/init.sql` 漏写了对应的 CREATE。请重建开发库"
            "（docker compose down -v && docker compose up -d postgres），"
            "或在生产上手工执行（存量表上建索引建议改成 CREATE INDEX CONCURRENTLY，"
            "它不能进事务块）：\n" + detail
        )
    logger.info("数据库结构已就绪")


async def _missing_columns() -> list[tuple[str, str]]:
    """模型有、库里没有的列，返回 `[(表名, 列名)]`。

    只查缺列不查多列：历史遗留的多列不影响正确性，而拒绝启动的代价远高于一次运行期 500。
    """
    from sqlalchemy import inspect as _inspect

    missing: list[tuple[str, str]] = []

    def compare(sync_connection):  # noqa: ANN001, ANN202
        inspector = _inspect(sync_connection)
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                missing.append((table.name, "<整张表缺失>"))
                continue
            existing = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name not in existing:
                    missing.append((table.name, column.name))

    async with engine.connect() as connection:
        await connection.run_sync(compare)
    return missing


async def _missing_indexes() -> list[tuple[str, str]]:
    """模型里显式命名的索引/唯一约束、库里没有的，返回 `[(名称, 建它用的 SQL)]`。

    只按**名称**比对：本仓库的索引与约束全部显式命名（匿名约束在 PG 侧是自动起名，无从比对），
    名字对不上就是漂移。丢失的后果都是静默的：`ux_sandbox_lease_one_per_user` 没了会让同一用户
    开出两台机器（重复计费），`uq_message_conversation_seq` 没了会让 `seq` 重号、跨设备顺序错乱。
    """
    expected = _expected_named_objects()

    def collect(sync_connection) -> set[str]:  # noqa: ANN001
        from sqlalchemy import inspect as _inspect

        inspector = _inspect(sync_connection)
        names: set[str] = set()
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                # 整表缺失已由 `_missing_columns` 报出，这里再报一遍只会淹没根因。
                continue
            names.update(index["name"] for index in inspector.get_indexes(table.name) if index.get("name"))
            names.update(item["name"] for item in inspector.get_unique_constraints(table.name) if item.get("name"))
        return names

    async with engine.connect() as connection:
        existing = await connection.run_sync(collect)
    return _missing_named_objects(expected, existing)


def _missing_named_objects(expected: dict[str, str], existing: set[str]) -> list[tuple[str, str]]:
    """`expected`（名称 → 建它用的 SQL）里未出现在 `existing` 的项，按名称排序。

    纯函数：没有数据库也能测（单测直接喂两个集合）。这是整个守卫唯一能被自动化验证的部分，
    比对逻辑留在这里而不是塞进 `run_sync` 的闭包里，就是为了让「缺索引 / 缺唯一约束 / 全齐」
    三种情形都有用例。排序只为让报错顺序稳定（集合的迭代顺序随版本变化，日志不好比对）。
    """
    return [(name, create_sql) for name, create_sql in sorted(expected.items()) if name not in existing]


def _expected_named_objects() -> dict[str, str]:
    """模型里显式命名的索引与唯一约束 → 建它用的 SQL（只用于报错文案，不执行）。"""
    preparer = postgresql.dialect().identifier_preparer
    expected: dict[str, str] = {}
    for table in Base.metadata.sorted_tables:
        quoted_table = preparer.format_table(table)
        for index in table.indexes:
            columns = ", ".join(_render_index_expression(expression) for expression in index.expressions)
            where = index.dialect_options["postgresql"].get("where")
            unique = "UNIQUE " if index.unique else ""
            suffix = f" WHERE {where}" if where is not None else ""
            expected[index.name] = (
                f"CREATE {unique}INDEX {preparer.quote(index.name)} ON {quoted_table} ({columns}){suffix};"
            )
        for constraint in table.constraints:
            if not isinstance(constraint, UniqueConstraint) or not constraint.name:
                continue
            columns = ", ".join(preparer.quote(column.name) for column in constraint.columns)
            expected[constraint.name] = (
                f"ALTER TABLE {quoted_table} ADD CONSTRAINT {preparer.quote(constraint.name)} UNIQUE ({columns});"
            )
    return expected


def _render_index_expression(expression) -> str:  # noqa: ANN001
    """索引列在 SQL 里的写法。`text()` 表达式（如 `created_at DESC`）原样保留：它是按字面 SQL 声明的。"""
    return getattr(expression, "name", None) or str(expression)
