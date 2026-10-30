"""结构守卫与建库脚本的一致性：零基础设施（不连库、不连网）。

`verify_schema_at_head` 的完整动作要有 PostgreSQL 才跑得起来，所以这里只锁两件能离线验证的事：
①索引/唯一约束的名称比对逻辑（纯函数，直接喂集合）；②模型 metadata 与 `sql/init.sql` 是同一份事实
——后者才是漂移的真正来源，而 CI 是空库，永远走不到「库已存在」那条分支。
"""

from __future__ import annotations

import inspect
import re
import typing
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, create_engine
from sqlalchemy.dialects import postgresql

import main
from app.db import base as db_base
from app.db import maintenance
from app.db import schema as db_schema
from app.db.base import Base
from app.db.models.provider import AUTH_STYLES as MODEL_AUTH_STYLES
from app.db.models.provider import ModelProvider
from app.db.models.sandbox import LEASE_STATES, SandboxLease
from app.db.models.sync import MESSAGE_ROLES, Conversation, Message
from app.db.models.user import User
from app.roles import ALL_ROLES
from app.schemas.sync import MessageIn
from app.services.metrics import metrics
from app.services.model_service.presets import AUTH_STYLES as PRESET_AUTH_STYLES
from app.services.sandbox import lease as lease_service

# ---------------------------------------------------------------------
# 名称比对（纯函数）：缺索引 / 缺唯一约束 / 全齐
# ---------------------------------------------------------------------
_INDEX_DDL = "CREATE UNIQUE INDEX ux_sandbox_lease_one_per_user ON sandbox_lease (user_id) WHERE state IN (...);"
_PLAIN_INDEX_DDL = "CREATE INDEX ix_message_pull ON message (user_id, version);"
_UNIQUE_DDL = "ALTER TABLE message ADD CONSTRAINT uq_message_conversation_seq UNIQUE (conversation_id, seq);"


def test_missing_named_objects_reports_missing_index():
    """丢了索引要报出来：`ux_sandbox_lease_one_per_user` 一消失，同一用户就能开出两台机器（重复计费）。"""
    expected = {"ux_sandbox_lease_one_per_user": _INDEX_DDL}

    assert db_schema._missing_named_objects(expected, set()) == [("ux_sandbox_lease_one_per_user", _INDEX_DDL)]


def test_missing_named_objects_reports_missing_unique_constraint():
    """唯一约束与索引走同一套比对：`uq_message_conversation_seq` 丢了会让 seq 重号、跨设备顺序错乱。"""
    expected = {"uq_message_conversation_seq": _UNIQUE_DDL, "ix_message_pull": _PLAIN_INDEX_DDL}

    missing = db_schema._missing_named_objects(expected, {"ix_message_pull"})

    assert [name for name, _ in missing] == ["uq_message_conversation_seq"]


def test_missing_named_objects_is_empty_when_everything_matches():
    """名字齐了就不许报：守卫误报的代价是「起不来」，比漏报更难被容忍。"""
    expected = {"ux_sandbox_lease_one_per_user": _INDEX_DDL, "uq_message_conversation_seq": _UNIQUE_DDL}

    assert db_schema._missing_named_objects(expected, set(expected)) == []


def test_expected_named_objects_covers_partial_index_and_composite_unique():
    """两项最容易被当成「普通索引」漏掉的结构：带谓词的部分唯一索引、复合唯一约束。

    它们没法用「列名集合」表达，所以只比对列的守卫对它们完全失明。
    """
    expected = db_schema._expected_named_objects()

    assert "WHERE state IN ('provisioning', 'ready', 'paused')" in expected["ux_sandbox_lease_one_per_user"]
    assert expected["uq_message_conversation_seq"].endswith("UNIQUE (conversation_id, seq);")
    # 保留字表名要带引号，否则复制粘贴出来的修复语句自己就报语法错。
    assert '"user"' in expected["ix_user_email"]


# ---------------------------------------------------------------------
# metadata 与 sql/init.sql 必须说同一件事
# ---------------------------------------------------------------------
def test_init_sql_declares_every_metadata_index_and_unique_constraint():
    """模型里声明的每个索引/唯一约束都要在建库脚本里出现。

    这是本模块最静默的漂移：在模型里加了索引却忘了改 `sql/init.sql`，本机重建库也建不出来，
    只能等线上启动时被守卫拒之门外。
    """
    init_sql = db_schema.init_sql_text()

    assert [name for name in db_schema._expected_named_objects() if name not in init_sql] == []


def test_init_sql_declares_no_index_the_models_do_not_know():
    """反向也要对上：脚本里建了模型不知道的索引，就是白付一份写放大且没人会发现。"""
    declared = set(re.findall(r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS (\w+)", db_schema.init_sql_text()))

    assert declared - set(db_schema._expected_named_objects()) == set()


def _check_values(table, constraint_name: str) -> tuple[str, ...]:  # noqa: ANN001
    """取出某个 CHECK 约束里列出的字面量取值。"""
    for constraint in table.constraints:
        if isinstance(constraint, CheckConstraint) and constraint.name == constraint_name:
            return tuple(re.findall(r"'([^']*)'", str(constraint.sqltext)))
    raise AssertionError(f"{table.name} 上没有名为 {constraint_name} 的 CHECK 约束")


@pytest.mark.parametrize(
    ("constraint_name", "table", "expected"),
    [
        # 状态机（services/sandbox/lease.py:STATE_*）：拼错的值会让部分唯一索引谓词不匹配。
        (
            "ck_sandbox_lease_state",
            SandboxLease.__table__,
            (
                lease_service.STATE_PROVISIONING,
                lease_service.STATE_READY,
                lease_service.STATE_PAUSED,
                lease_service.STATE_DESTROYING,
                lease_service.STATE_DESTROYED,
                lease_service.STATE_FAILED,
                lease_service.STATE_DESTROY_FAILED,
            ),
        ),
        # 前端按 role 分支渲染（app/schemas/sync.py:MessageIn.role）。
        ("ck_message_role", Message.__table__, tuple(typing.get_args(MessageIn.model_fields["role"].annotation))),
        # 三档 RBAC 的唯一事实来源（app/roles.py:ALL_ROLES）。
        ("ck_user_role", User.__table__, ALL_ROLES),
        # 请求头名字（services/model_service/presets.py:AUTH_STYLES）。
        ("ck_model_provider_auth_style", ModelProvider.__table__, tuple(MODEL_AUTH_STYLES)),
    ],
)
def test_enum_check_values_match_python_constants(constraint_name, table, expected):  # noqa: ANN001
    """CHECK 的取值必须与 Python 常量逐字一致：两份取值一旦分叉，库会拒绝合法值或放过错值。"""
    assert set(_check_values(table, constraint_name)) == set(expected)


def test_model_enum_constants_do_not_drift_from_services():
    """模型里为 CHECK 复制的常量，必须与真正使用它们的服务层常量一致。"""
    assert set(LEASE_STATES) == {
        lease_service.STATE_PROVISIONING,
        lease_service.STATE_READY,
        lease_service.STATE_PAUSED,
        lease_service.STATE_DESTROYING,
        lease_service.STATE_DESTROYED,
        lease_service.STATE_FAILED,
        lease_service.STATE_DESTROY_FAILED,
    }
    assert set(MODEL_AUTH_STYLES) == PRESET_AUTH_STYLES
    assert set(MESSAGE_ROLES) == set(typing.get_args(MessageIn.model_fields["role"].annotation))


def test_init_sql_carries_every_model_check_constraint():
    """`init.sql` 里的 CHECK 必须逐字（去掉换行空白后）与模型一致。

    空库首建只跑脚本、结构比对只读 metadata，两者任何一边漏改都不会有运行期症状。
    """
    normalized_sql = re.sub(r"\s+", " ", db_schema.init_sql_text())
    missing = [
        constraint.name
        for table in Base.metadata.sorted_tables
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
        and re.sub(r"\s+", " ", str(constraint.sqltext)) not in normalized_sql
    ]

    assert missing == []


def test_message_binds_user_id_to_its_conversation_by_composite_foreign_key():
    """`message.user_id` 是冗余列，只能靠复合外键和会话归属绑死。

    两条互相独立的 FK 时它退化成一列普通 UUID：任何一条写入路径漏传/传错，`pull`
    （按 `Message.user_id` 过滤）就会把别人的消息投给错误租户。
    """
    assert "uq_conversation_id_user" in {constraint.name for constraint in Conversation.__table__.constraints}

    (foreign_key,) = [
        constraint for constraint in Message.__table__.constraints if isinstance(constraint, ForeignKeyConstraint)
    ]

    assert foreign_key.name == "fk_message_conversation_user"
    assert [column.name for column in foreign_key.columns] == ["conversation_id", "user_id"]
    assert [element.target_fullname for element in foreign_key.elements] == ["conversation.id", "conversation.user_id"]
    # 删 user → 级联删 conversation → 再级联删 message，消息不会因为旧 FK 被拆掉而失去级联。
    assert foreign_key.ondelete == "CASCADE"


# ---------------------------------------------------------------------
# 连接池：会话级超时与 checkout 计时
# ---------------------------------------------------------------------
@pytest.mark.parametrize("pool_mode", ["pool", "null"])
def test_engine_kwargs_pins_session_level_timeouts(monkeypatch, pool_mode):
    """两个会话级超时由连接层下发（PG 文档不建议写 postgresql.conf：会影响所有会话）。

    池模式不同也必须都带上：运维脚本走 NullPool，同样会开事务等外部 I/O。
    """
    monkeypatch.setattr(db_base.settings, "database_pool_mode", pool_mode)

    server_settings = db_base._engine_kwargs()["connect_args"]["server_settings"]  # type: ignore[index]

    assert server_settings["idle_in_transaction_session_timeout"] == str(db_base.IDLE_IN_TRANSACTION_TIMEOUT_MS)
    assert server_settings["lock_timeout"] == str(db_base.LOCK_TIMEOUT_MS)
    # 等锁必须短于语句超时：一次等锁会耗光整个 statement 预算，表面上却是「查询慢」。
    assert int(server_settings["lock_timeout"]) < int(server_settings["statement_timeout"])


def test_module_engine_has_the_checkout_timer_attached():
    """应用进程用的就是模块级 `engine`：计时器必须挂在它的池上，且只挂一份。

    挂两份会把每次 checkout 记两遍，分位数看着对、总量翻倍；而 `build_engine()` 造的是另一份引擎，
    这里检查它不能作为替代。
    """
    assert len(db_base.engine.pool.dispatch.checkout) == 1
    assert len(db_base.engine.pool.dispatch.checkin) == 1


def test_pool_checkout_duration_is_observed_on_checkin():
    """checkout→checkin 的时长真的落进 `db_pool_checkout_seconds`。

    用内存 sqlite 引擎跑同一条事件路径（事件名与回调签名和 PG 引擎完全一致），所以不需要任何服务。
    """
    sqlite_engine = create_engine("sqlite://")
    db_base.instrument_pool_checkout(sqlite_engine)
    metrics.reset()

    with sqlite_engine.connect():
        pass
    sqlite_engine.dispose()

    samples = [sample for sample in metrics.snapshot() if sample.name == db_base.DB_POOL_CHECKOUT_SECONDS]
    metrics.reset()

    assert len(samples) == 1
    assert samples[0].count == 1
    assert samples[0].sum_seconds >= 0


# ---------------------------------------------------------------------
# 沙箱历史保留期
# ---------------------------------------------------------------------
class _RecordingSession:
    """记录 execute 到的语句的替身会话（不连库）。"""

    def __init__(self, rowcount: int = 3) -> None:
        self.statements = []
        self.rowcount = rowcount
        self.committed = False

    async def __aenter__(self) -> _RecordingSession:
        return self

    async def __aexit__(self, *exc_info) -> bool:  # noqa: ANN002
        return False

    async def execute(self, statement):  # noqa: ANN001, ANN201
        self.statements.append(statement)
        return SimpleNamespace(rowcount=self.rowcount)

    async def commit(self) -> None:
        self.committed = True


async def test_cleanup_sandbox_history_deletes_only_terminal_leases_past_retention(monkeypatch):
    """只删「已结束 + 过了保留期」的租约，并且按 `destroyed_at` 判保留期。"""
    session = _RecordingSession(rowcount=3)
    monkeypatch.setattr(maintenance, "async_session_maker", lambda: session)

    removed = await maintenance.cleanup_sandbox_history(older_than_days=7)

    assert removed == 3
    assert session.committed
    (statement,) = session.statements
    compiled = statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "DELETE FROM sandbox_lease" in sql
    assert "destroyed_at <" in sql
    # 三种终态都带上了；活动状态（ready 等）一个都不能出现在参数里。
    (states,) = [value for key, value in compiled.params.items() if key.startswith("state_")]
    assert set(states) == set(maintenance.TERMINAL_LEASE_STATES)
    (cutoff,) = [value for key, value in compiled.params.items() if key.startswith("destroyed_at_")]
    assert abs(cutoff - (datetime.now(timezone.utc) - timedelta(days=7))) < timedelta(minutes=1)


@pytest.mark.parametrize("days", [0, -1])
async def test_cleanup_sandbox_history_rejects_non_positive_retention(days):
    """保留期 0/负值等于「清空全部历史」，与过期会话清理同一条底线：拒绝而不是尽力而为。"""
    with pytest.raises(ValueError, match="必须 > 0"):
        await maintenance.cleanup_sandbox_history(days)


def test_terminal_lease_states_match_lease_service():
    """维护动作里的终态集合必须与状态机常量一致（两份都在用，任一边改名都会让清理失效）。"""
    assert set(maintenance.TERMINAL_LEASE_STATES) == {
        lease_service.STATE_DESTROYED,
        lease_service.STATE_FAILED,
        lease_service.STATE_DESTROY_FAILED,
    }


def test_run_all_leaves_schema_verification_to_the_lifespan():
    """入口只跑一次建库脚本：`app.server:lifespan` 已覆盖 `uvicorn app.server:app` 直启。"""
    assert "verify_schema_at_head" not in inspect.getsource(main._run_all)
