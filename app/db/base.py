"""数据库基座：ORM 基类、加密列、引擎与连接池参数。

只放「任何表都依赖」的东西；表定义在 `app/db/models/`，对外统一由 `app.db` 暴露。
"""

import logging
from pathlib import Path

from sqlalchemy import Text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

from app.config import settings
from app.config.secret_box import SecretDecryptError, decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)

#: 项目根目录。sql/init.sql 锚在这里，不能用 CWD：从别的目录启动会找不到建库脚本。
#: 本文件比原先的 app/db.py 深一层，故多退一级（parents[2] 才是仓库根）；所有模块从这里取，避免各算一份。
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


# ---------------------------------------------------------------------
# 引擎与会话
# ---------------------------------------------------------------------
DATABASE_URL = settings.database_url


def _engine_kwargs() -> dict[str, object]:
    """引擎参数。单独成函数是为了让测试能构造同参数的独立引擎。

    `pool_pre_ping` 挡住死连接（重启/断连），否则是偶发 500；`application_name` 供库里区分本服务。
    `statement_timeout` 让服务端真取消超时查询：`wait_for` 只放弃等待，烂查询仍占连接。
    """
    from sqlalchemy.pool import NullPool

    server_settings = {"application_name": "bennett-agent"}
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
    return create_async_engine(url or DATABASE_URL, **_engine_kwargs())  # type: ignore[arg-type]
