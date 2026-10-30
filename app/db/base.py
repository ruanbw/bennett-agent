"""数据库基座：ORM 基类、加密列、引擎与连接池参数。

只放「任何表都依赖」的东西；表定义在 `app/db/models/`，对外统一由 `app.db` 暴露。
"""

import logging
import time
from pathlib import Path

from sqlalchemy import CheckConstraint, Text, event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

from app.config import settings
from app.config.secret_box import SecretDecryptError, decrypt_secret, encrypt_secret
from app.services.metrics import metrics

logger = logging.getLogger(__name__)

#: 项目根目录。sql/init.sql 锚在这里，不能用 CWD：从别的目录启动会找不到建库脚本。
#: 本文件在 `app/db/` 下层，仓库根是 parents[2]；所有模块从这里取，避免各算一份。
PROJECT_ROOT = Path(__file__).resolve().parents[2]


#: 告警只打一次：列表端点与每次对话都读这一列，不去重会刷屏到根因被淹没。
_DECRYPT_FAILURE_REPORTED = False


class EncryptedText(TypeDecorator):
    """透明的「落库加密、读取解密」文本列（API Key 用）。

    业务代码继续当明文读写，加密只在 bind/result 边界发生；解不开时返回 None——这层拿不到
    行上下文，抛出去会让整个后台 500；要知道原因用 `audit_provider_key_decryption()`。
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):  # noqa: ANN001, ANN201
        if value is None:
            return None
        return encrypt_secret(str(value))

    def process_result_value(self, value, dialect):  # noqa: ANN001, ANN201
        if value is None:
            return None
        try:
            return decrypt_secret(str(value))
        except SecretDecryptError:
            global _DECRYPT_FAILURE_REPORTED
            if not _DECRYPT_FAILURE_REPORTED:
                _DECRYPT_FAILURE_REPORTED = True
                logger.error(
                    "有提供商的 API Key 解密失败：AUTH_SECRET 可能已更换。"
                    "受影响的行会以「未配置密钥」呈现；调用 /health/ready 可看到具体是哪几个。",
                    exc_info=True,
                )
            return None


class Base(DeclarativeBase):
    pass


def enum_check(column: str, values: tuple[str, ...], *, name: str) -> CheckConstraint:
    """把枚举列的合法取值变成 `CHECK` 约束：Python 常量是事实来源，这里只是把它落到 DDL。

    库层兜底的理由是失败方式：`sandbox_lease.state` 写错一个字母时，部分唯一索引的谓词不再匹配，
    同一用户会开出第二台机器且不报错；`message.role` 写错则前端渲染静默走默认分支。取值用字面量
    拼进 SQL（这里是编译期常量，不是运行期输入）。线上补加这类约束要先 `ADD CONSTRAINT ... NOT VALID`
    再单独 `VALIDATE CONSTRAINT`，否则一次加约束会拿 ACCESS EXCLUSIVE 锁扫全表。
    """
    joined = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({joined})", name=name)


# ---------------------------------------------------------------------
# 引擎与会话
# ---------------------------------------------------------------------
DATABASE_URL = settings.database_url

#: 事务里空闲超过这么久就让服务端掐掉会话（毫秒）。停在 `idle in transaction` 的事务会一直持锁并
#: 阻止 autovacuum 回收死行；本仓库的建库脚本与条件 UPDATE 都挤在同几张表上，一次卡住的空闲事务
#: 能让 `mark_destroying` 排队到 `statement_timeout` 之后才失败。写成常量而非 settings 字段：
#: 这是任何部署都不该放开的下限，不是部署差异。
IDLE_IN_TRANSACTION_TIMEOUT_MS = 60_000

#: 等锁超时（毫秒）。没有它，一条等锁的 `CREATE INDEX` / `ALTER TABLE` 会经由锁队列把后续所有写
#: 一起堵住——即使它自己最终会因为超时放弃。
LOCK_TIMEOUT_MS = 3_000


def _engine_kwargs() -> dict[str, object]:
    """引擎参数。单独成函数是为了让测试能构造同参数的独立引擎。

    `pool_pre_ping` 挡住死连接（重启/断连），否则是偶发 500；`application_name` 供库里区分本服务。
    三个超时都按会话下发而不是写进 `postgresql.conf`：PG 官方文档明确不建议写在配置文件里，
    因为那会连运维手工敲的 `psql` 会话一起限时（跑 ALTER 的人正需要拿长锁）。
    `statement_timeout` 让服务端真取消超时查询：`wait_for` 只放弃等待，烂查询仍占连接。
    """
    from sqlalchemy.pool import NullPool

    server_settings = {
        "application_name": "bennett-agent",
        "idle_in_transaction_session_timeout": str(IDLE_IN_TRANSACTION_TIMEOUT_MS),
        "lock_timeout": str(LOCK_TIMEOUT_MS),
    }
    if settings.database_statement_timeout_ms > 0:
        server_settings["statement_timeout"] = str(settings.database_statement_timeout_ms)

    if settings.database_pool_mode == "null":
        return {
            "poolclass": NullPool,
            "pool_pre_ping": False,
            "connect_args": {
                "server_settings": server_settings,
                "timeout": settings.database_connect_timeout,
            },
        }

    return {
        "pool_size": settings.database_pool_size,
        "max_overflow": settings.database_max_overflow,
        "pool_timeout": settings.database_pool_timeout,
        "pool_recycle": settings.database_pool_recycle,
        "pool_pre_ping": True,
        "connect_args": {
            "server_settings": server_settings,
            # 建连超时（asyncpg 默认 60s）：数据库不可达时不该让请求挂一分钟
            "timeout": settings.database_connect_timeout,
        },
    }


engine = create_async_engine(DATABASE_URL, **_engine_kwargs())  # type: ignore[arg-type]
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


def build_engine(url: str | None = None):  # noqa: ANN201 - 返回值是 AsyncEngine
    """按当前配置（或指定 URL）构造一个引擎。测试与运维脚本用。"""
    built = create_async_engine(url or DATABASE_URL, **_engine_kwargs())  # type: ignore[arg-type]
    instrument_pool_checkout(built.sync_engine)
    return built


# ---------------------------------------------------------------------
# 连接池可观测
# ---------------------------------------------------------------------
#: 一次 checkout 从借出到归还的时长（秒）。它回答的是「池为什么打满」：池耗尽的唯一症状是
#: `pool_timeout` 之后的 500，而 `pool_timeout`（默认 10s）与 `statement_timeout`（30s）两种 500
#: 在日志里长得一模一样，只有「连接被占住了多久」能把它们分开。
#: 指标名定义在产出地而不是 `services/metrics.py`：与 `services/redis_client.py` 的
#: `redis_unavailable_total` 同套路，只在被度量的那一处出现一次。
DB_POOL_CHECKOUT_SECONDS = "db_pool_checkout_seconds"

_CHECKOUT_STARTED_KEY = "bennett_checkout_started_at"


def instrument_pool_checkout(target) -> None:  # noqa: ANN001 - target 是同步 Engine
    """给引擎挂上 checkout/checkin 计时；抽成函数是为了测试能对无网络的 sqlite 引擎挂同一份实现。

    计时的两端只能是这两个事件：拿 `AsyncSession` 本身不建连（惰性，第一条语句才 checkout），
    所以在会话层量不到任何东西。
    """

    @event.listens_for(target, "checkout")
    def _on_checkout(dbapi_connection, connection_record, connection_proxy) -> None:  # noqa: ANN001
        connection_record.info[_CHECKOUT_STARTED_KEY] = time.monotonic()

    @event.listens_for(target, "checkin")
    def _on_checkin(dbapi_connection, connection_record) -> None:  # noqa: ANN001
        started = connection_record.info.pop(_CHECKOUT_STARTED_KEY, None)
        # 没有起点通常是引擎在挂监听之前就借出过连接；宁可不记也不要记一个假的 0。
        if started is None:
            return
        metrics.observe(DB_POOL_CHECKOUT_SECONDS, time.monotonic() - started)


instrument_pool_checkout(engine.sync_engine)
