"""数据库基础设施：异步引擎、ORM 基类，以及业务表定义。

表结构的唯一事实来源是 sql/init.sql：整段幂等，启动时执行；其 CREATE TABLE IF NOT EXISTS
对既有表不加列，故启动时逐列比对、漂移即拒绝启动。手写 SQL 里 `user` 必须加双引号。

拆成包是为了让「表定义」与「引擎/维护/依赖注入」各自可读；对外仍只暴露 `app.db` 这一层，
调用方不需要知道名字在哪个子模块。这里必须把每个模型模块都 import 过：SQLAlchemy 是按字符串
解析 relationship 的，少 import 一个模块，mapper 就会在运行期（配置 mapper 时）才炸。

`__all__` 与拆分前完全一致。不在 `__all__` 里、但一直被 `from app.db import X` 取用的名字
（沙箱模型、`init_sql_text`、会话缓存函数等）就地标 `noqa: F401` 继续导出：它们是对外公开面，
删掉等于在重构里悄悄改 API。
"""

from app.db.base import (
    DATABASE_URL,
    PROJECT_ROOT,  # noqa: F401
    Base,
    EncryptedText,  # noqa: F401
    _engine_kwargs,  # noqa: F401
    async_session_maker,
    build_engine,
    engine,
)
from app.db.deps import get_access_token_db, get_async_session, get_user_db
from app.db.maintenance import (
    audit_provider_key_decryption,  # noqa: F401
    cleanup_expired_access_tokens,
    delete_user_access_tokens,
)
from app.db.models.provider import AppSetting, ModelProvider, ProviderModel
from app.db.models.sandbox import SandboxCommand, SandboxLease  # noqa: F401
from app.db.models.sync import Conversation, Message, SyncCounter
from app.db.models.user import AccessToken, HashedAccessTokenDatabase, User, hash_access_token
from app.db.schema import (
    INIT_SQL_PATH,  # noqa: F401
    _missing_columns,  # noqa: F401
    init_sql_text,  # noqa: F401
    verify_schema_at_head,
)
from app.db.session_cache import (
    _session_cache_delete,  # noqa: F401
    _session_cache_get,  # noqa: F401
    _session_cache_set,  # noqa: F401
    _session_cache_ttl,  # noqa: F401
)

__all__ = [
    "DATABASE_URL",
    "AccessToken",
    "AppSetting",
    "Base",
    "Conversation",
    "HashedAccessTokenDatabase",
    "Message",
    "ModelProvider",
    "ProviderModel",
    "SyncCounter",
    "User",
    "async_session_maker",
    "build_engine",
    "cleanup_expired_access_tokens",
    "delete_user_access_tokens",
    "engine",
    "get_access_token_db",
    "get_async_session",
    "get_user_db",
    "hash_access_token",
    "verify_schema_at_head",
]
