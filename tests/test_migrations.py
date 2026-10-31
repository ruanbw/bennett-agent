"""迁移脚手架的离线测试：零基础设施（不连库、不连网）。

锁四件事：①迁移链是一条单链且头就是 baseline；②upgrade() 在「业务表已存在」分支只 stamp、
「表不存在」分支报错引导跑 init.sql（用替身 connection 模拟，不碰 PostgreSQL）；③downgrade()
只删版本行、不碰业务表；④env.py 的两个关键绑定用 AST 断言——target_metadata 必须赋值为
app.db 的 Base.metadata（指到空 metadata 会安静产出错误脚本），连接串必须取自
settings.database_url（alembic.ini 不落第二份）。env.py 本身不 import：alembic.context
只在 alembic 初始化好的运行环境里有意义，exec 它反而是在测框架而不是测我们写的东西。
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _script_directory() -> ScriptDirectory:
    """与 alembic.ini 等价的编程式配置，只用于读取迁移链结构。"""
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return ScriptDirectory.from_config(cfg)


def test_migration_chain_is_single_linear_chain_with_baseline_head():
    """迁移链必须是一条单链（无分叉多头），且唯一的头是 baseline。"""
    script = _script_directory()

    assert script.get_heads() == ["0001_baseline"]

    # walk 从 base 到 head 恰好一个 revision，基线没有 down_revision。
    walked = list(script.walk_revisions())
    assert [rev.revision for rev in walked] == ["0001_baseline"]
    assert walked[0].down_revision is None


def test_baseline_module_exports_alembic_identifiers():
    """迁移文件就是 Alembic 的加载单元：revision 标识符必须可被直接 import 并与链上一致。"""
    module = importlib.import_module("migrations.versions.0001_baseline")

    assert module.revision == "0001_baseline"
    assert module.down_revision is None


class _RecordingBind:
    """记录 execute 收到的语句的替身 connection（scalar 走构造时的预设值）。"""

    def __init__(self, scalar_value: bool) -> None:
        self.scalar_value = scalar_value
        self.statements: list[str] = []

    def execute(self, statement):  # noqa: ANN001, ANN201
        self.statements.append(str(statement))
        return SimpleNamespace(scalar=lambda: self.scalar_value)


def test_upgrade_stamps_when_business_tables_exist():
    """表已在（init.sql 已跑过）：创建版本表并 stamp，不抛错、不产出任何业务 DDL。"""
    baseline = importlib.import_module("migrations.versions.0001_baseline")
    recorded = _RecordingBind(True)

    with patch.object(baseline.op, "get_bind", return_value=recorded):
        baseline.upgrade()

    assert len(recorded.statements) == 2
    assert "to_regclass" in recorded.statements[0]
    assert "CREATE TABLE IF NOT EXISTS alembic_version" in recorded.statements[1]
    assert "0001_baseline" in recorded.statements[1]


def test_upgrade_raises_and_points_to_init_sql_when_tables_absent():
    """空库/新库：报错并引导先跑 sql/init.sql——Alembic 不接管首建，两套建表并存是漂移之源。"""
    baseline = importlib.import_module("migrations.versions.0001_baseline")
    recorded = _RecordingBind(False)

    with patch.object(baseline.op, "get_bind", return_value=recorded), pytest.raises(RuntimeError, match="init.sql"):
        baseline.upgrade()

    # 报错分支不许有任何 DDL 副作用：只有那一条探针查询。
    assert len(recorded.statements) == 1
    assert "to_regclass" in recorded.statements[0]


def test_downgrade_deletes_only_the_version_row():
    """downgrade 只删版本行：业务表的事实来源在 init.sql，Alembic 无权回滚它。"""
    baseline = importlib.import_module("migrations.versions.0001_baseline")
    recorded = _RecordingBind(True)

    with patch.object(baseline.op, "get_bind", return_value=recorded):
        baseline.downgrade()

    assert len(recorded.statements) == 1
    assert recorded.statements[0] == "DELETE FROM alembic_version WHERE version_num = '0001_baseline'"


def _env_py_source() -> str:
    return (PROJECT_ROOT / "migrations" / "env.py").read_text(encoding="utf-8")


def test_env_py_targets_app_db_metadata():
    """env.py 的 target_metadata 必须赋值为 app.db 的 Base.metadata。

    指到别的（空）metadata 上，autogenerate 会安静地产出「从零建全库」的错误脚本。
    用 AST 断言而不是 import env.py：`alembic.context` 只在 alembic 初始化好的运行环境里
    有 config 可言，在 pytest 里 exec 它测的是框架初始化而不是我们写的东西。
    """
    tree = ast.parse(_env_py_source())

    imports_base = any(
        isinstance(node, ast.ImportFrom) and node.module == "app.db" and any(a.name == "Base" for a in node.names)
        for node in ast.walk(tree)
    )
    targets_metadata = any(
        isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "target_metadata" for t in node.targets)
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "Base"
        and node.value.attr == "metadata"
        for node in ast.walk(tree)
    )

    assert imports_base
    assert targets_metadata


def test_env_py_uses_settings_database_url():
    """连接串取自 app.config.settings（唯一配置真相），alembic.ini 里不落第二份。"""
    tree = ast.parse(_env_py_source())

    imports_settings = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "app.config"
        and any(a.name == "settings" for a in node.names)
        for node in ast.walk(tree)
    )
    binds_url = 'set_main_option("sqlalchemy.url", settings.database_url)' in _env_py_source()

    assert imports_settings
    assert binds_url
