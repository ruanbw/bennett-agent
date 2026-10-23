"""Alembic 环境：用应用的 Settings 构造连接，不做第二份配置。

## 为什么是 async 模板

应用用的是 `postgresql+asyncpg`，而 Alembic 的默认模板是同步 Engine。如果为迁移
单独配一条 `psycopg2` 连接串，就多出一份必须与 `DATABASE_URL` 保持同步的配置，也就
多出一种「迁移跑到了另一个库」的故障。这里直接复用应用设置，走异步引擎。

## `compare_type` / `compare_server_default` 为什么必须开

默认的 autogenerate 只比对「表/列的存在与否」。改了列类型（`String(20)` →
`String(40)`）或默认值却忘了写迁移时，它什么都不报，而运行期的表现为截断或约束冲突。
打开这两项之后，`alembic check` 能在 CI 里直接把漂移拦下来。

## 禁止在迁移里 import 应用模型

迁移脚本只描述**当时那一刻**的结构。引用 `app.db.User` 之类会随代码演进的符号，
会让「历史迁移在新代码上重新执行」得到不同的结果——那正是 Alembic 要避免的事。
`target_metadata` 只用于 autogenerate 的**比对**，不作为迁移脚本的依赖。
"""

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# 让 `alembic` 在仓库根之外被调用时也能 import app.*（alembic.ini 里已设
# prepend_sys_path，这里再兜一层：有些调用方式不会读那一项）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.db import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    # `disable_existing_loggers=False` 是必需的，不是为了好看：
    # fileConfig 默认会把**所有已存在的 logger 置为 disabled**。在测试里跑迁移时，
    # 那会把 pytest 的 caplog handler 一起干掉——表现为「所有涉及日志断言的用例
    # 突然取到空字符串」，而失败点看起来跟迁移毫无关系。
    # 在部署进程里它同样有害：迁移与业务代码共用一套 logger 配置才是可预期的行为。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

#: autogenerate 的比对目标。迁移脚本本身**不得**依赖它（见模块 docstring）。
target_metadata = Base.metadata


#: 渲染这些类型时用标准 SQLAlchemy 类型，而不是带模块路径的 import。
#:
#: 默认情况下 autogenerate 会把自定义 TypeDecorator 渲染成
#: `fastapi_users_db_sqlalchemy.generics.GUID()` / `app.db.EncryptedText()`，却**不写
#: 对应的 import**——生成的迁移文件一执行就 NameError。而让迁移去 import 应用模型
#: 又违反了「迁移不依赖会随代码演进的符号」这条原则。
#:
#: 所以这里把它们降级成它们在 Postgres 上的真实 DDL 类型：GUID → UUID、
#: EncryptedText → TEXT（加密在应用层，列类型就是普通文本）、TIMESTAMPAware →
#: TIMESTAMPTZ。迁移只描述结构，不携带业务实现。
def render_item(type_: str, obj, autogen_context) -> str | bool:  # noqa: ANN001
    if type_ != "type":
        return False

    from fastapi_users_db_sqlalchemy.generics import GUID, TIMESTAMPAware

    if isinstance(obj, GUID):
        return "sa.Uuid()"
    if isinstance(obj, TIMESTAMPAware):
        return "sa.DateTime(timezone=True)"

    from app.db import EncryptedText

    if isinstance(obj, EncryptedText):
        return "sa.Text()"
    return False


#: 连接串只有一个来源：应用配置。
config.set_main_option("sqlalchemy.url", settings.database_url)


def run_migrations_offline() -> None:
    """离线模式：只输出 SQL，不连库（`alembic upgrade head --sql`）。

    运维有时需要把 DDL 交给 DBA 审核后再执行。离线模式下 `literal_binds` 必须开：
    否则参数占位符会原样出现在输出里，那份 SQL 是跑不了的。
    """
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_item=render_item,
    )

    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:  # noqa: ANN001 - Alembic 传入的同步连接
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        render_item=render_item,
        # 版本表用默认名 `alembic_version`：改名不会带来任何好处，
        # 只会让运维脚本里多一个需要知道的常量。
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
