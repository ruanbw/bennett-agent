"""运行时 schema 守卫：执行 sql/init.sql，并逐列比对模型与库，漂移即拒绝启动。"""

import logging

from sqlalchemy import text

from app.config import settings
from app.db.base import PROJECT_ROOT, Base, engine

logger = logging.getLogger(__name__)

#: schema 唯一事实来源：整段幂等，可直接重复执行。
INIT_SQL_PATH = PROJECT_ROOT / "sql" / "init.sql"


def init_sql_text() -> str:
    return INIT_SQL_PATH.read_text(encoding="utf-8")


async def verify_schema_at_head() -> None:
    """执行 sql/init.sql 建表，然后逐列比对模型与库，漂移就拒绝启动。

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
