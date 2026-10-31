"""baseline：与 sql/init.sql 建好的库对齐

Revision ID: 0001_baseline
Revises: —
Create Date: 2026-11-01

策略（为什么这样写）：
- 本仓库的表结构唯一事实来源是 `sql/init.sql`：启动守卫执行它并逐列/逐索引比对模型，漂移即拒绝启动。
  Alembic 只负责「生产库的增量升级路径」，不接管建表。
- 开发/新库路径：启动时 `init.sql` 已把表建好（幂等），随后 Alembic 只需把版本表 `alembic_version`
  打到 baseline——表已经在，不需要任何 DDL。
- 生产增量路径：以后改表 = 先改 `sql/init.sql`（保持唯一事实来源）+ 同步新增一个增量迁移脚本，
  两者一起提交；生产库通过 `alembic upgrade head` 应用增量。
- 因此 upgrade() 幂等双分支：业务表已在 → 只建 `alembic_version` 并 stamp；业务表不在 → 报错引导
  先跑 init.sql（新库首建走启动流程，Alembic 不接管首建，避免两套建表并存）。
- downgrade() 只丢弃版本记录，不删业务表：首建事实来源在 init.sql，Alembic 无权回滚它。
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 判断「库是否已由 init.sql 建过」的探针表：init.sql 的第一张业务表。
PROBE_TABLE = "app_setting"

#: 打版本戳的 DDL。`CREATE TABLE IF NOT EXISTS` + `ON CONFLICT DO UPDATE`：重复 stamp 幂等。
STAMP_SQL = f"""\
CREATE TABLE IF NOT EXISTS alembic_version (
    version_num VARCHAR(32) NOT NULL, PRIMARY KEY (version_num)
);
INSERT INTO alembic_version (version_num)
VALUES ('{revision}')
ON CONFLICT (version_num) DO UPDATE SET version_num = EXCLUDED.version_num;
"""


def upgrade() -> None:
    """双分支幂等：表已在 → stamp；表不在 → 报错引导先跑 init.sql。"""
    connection = op.get_bind()
    exists = connection.execute(text("SELECT to_regclass('public.app_setting') IS NOT NULL")).scalar()
    if not exists:
        raise RuntimeError(
            "业务表不存在：这不是空库迁移场景。请先启动应用让启动守卫执行 sql/init.sql 建表"
            "（新库：docker compose up -d postgres 后 `uv run uvicorn app.server:app` 启动一次），"
            "再运行 alembic stamp head / alembic upgrade head。"
        )
    connection.execute(text(STAMP_SQL))


def downgrade() -> None:
    """只撤销版本记录，不动业务表（首建事实来源是 sql/init.sql）。"""
    connection = op.get_bind()
    connection.execute(text("DELETE FROM alembic_version WHERE version_num = '0001_baseline'"))
