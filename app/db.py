"""数据库基础设施：异步引擎、ORM 基类，以及用户与会话两张表。

## 为什么是 SQLite

本项目的写入压力很低：流式对话按 800ms 节流落库，限流上限 30 请求/分/账号，
单进程部署。即使 100 路对话同时进行也只有约 125 次单行写/秒，SQLite（WAL 模式）
的余量在两三个数量级。存储量同理，远未接近其上限。

**唯一会迫使换库的情况是多机部署**——SQLite 文件是本地的，多实例会各写各的。
届时换 Postgres 只需改 `database_url` 与这里的引擎构造。

## 两张表的职责

- `user`：fastapi-users 的标准用户表（邮箱 + 密码哈希 + 三个布尔位）。
- `access_token`：**服务端会话**。一条记录 = 一个登录态。这是「不用 JWT」的落点：
  token 只是一串随机值，状态在库里，因此登出/踢人/改密失效都是删一行，立即生效。
"""

import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends
from fastapi_users.db import SQLAlchemyBaseUserTableUUID, SQLAlchemyUserDatabase
from fastapi_users_db_sqlalchemy.access_token import (
    SQLAlchemyAccessTokenDatabase,
    SQLAlchemyBaseAccessTokenTableUUID,
)
from sqlalchemy import delete, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class User(SQLAlchemyBaseUserTableUUID, Base):
    """用户表。

    字段由 fastapi-users 定义：`id`(UUID) / `email` / `hashed_password` /
    `is_active` / `is_superuser` / `is_verified`。
    本项目不做权限分级，因此 `is_superuser` 只作为预留位，未参与任何判断。
    """


class AccessToken(SQLAlchemyBaseAccessTokenTableUUID, Base):
    """会话表：`token` / `user_id` / `created_at`。

    `user_id` 带 `ondelete="cascade"`，但 SQLite 默认**不启用**外键约束，
    所以下面必须显式 `PRAGMA foreign_keys=ON`，否则删用户后会话记录会残留。
    """


def _resolve_database_url(url: str) -> str:
    """把相对的 SQLite 路径固定到项目根目录。

    `sqlite+aiosqlite:///./bennett.db` 是相对**当前工作目录**解析的。从别的目录
    启动（例如 `python /path/to/main.py` 或在 IDE 里跑测试）就会悄悄创建出第二个
    空库，表现为「注册过的账号不见了」。这里统一锚到 `BASE_DIR`。
    """
    prefix = "sqlite+aiosqlite:///"
    if not url.startswith(prefix):
        return url

    raw_path = url[len(prefix) :]
    # 已是绝对路径（/xxx），或内存库（:memory:），都原样返回
    if raw_path.startswith("/") or raw_path.startswith(":memory:"):
        return url

    absolute = (Path(__file__).resolve().parent.parent / raw_path).resolve()
    return f"{prefix}{absolute}"


DATABASE_URL = _resolve_database_url(settings.database_url)

engine = create_async_engine(DATABASE_URL)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


if engine.dialect.name == "sqlite":

    @event.listens_for(engine.sync_engine, "connect")
    def _apply_sqlite_pragmas(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
        """SQLite 的 PRAGMA 是**每连接**生效，不是数据库级的持久设置。

        - `foreign_keys=ON`：SQLite 默认关闭外键约束，不开则级联删除失效。
        - `journal_mode=WAL`：读写不互相阻塞，避免流式对话期间的写锁卡住读请求。
        - `busy_timeout=5000`：并发写入时排队等待而不是立刻抛 `database is locked`。
        """
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


async def create_db_and_tables() -> None:
    """启动时建表。

    **没有引入 Alembic**：当前还没有「改已有列」的迁移需求，多一套工具链不划算。
    注意 `create_all` 只创建缺失的表，**不会修改已存在的表结构**；一旦需要给现有
    表加列，应改用 Alembic，而不是指望这里生效。
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("数据库就绪: %s", DATABASE_URL)


# ---------------------------------------------------------------------
# 会话维护
# ---------------------------------------------------------------------
async def cleanup_expired_access_tokens(lifetime_seconds: int) -> int:
    """删除已过期的会话行，返回删除条数。

    为什么需要显式清理：`DatabaseStrategy.read_token` 只把 `created_at` 当**过滤条件**
    （`get_by_token(token, max_age)` 是 `WHERE created_at >= max_age` 的 SELECT），
    **从不删除过期行**。没有这个维护动作，`access_token` 表会随登录次数无限增长，
    已过期的会话 token 也长期留在库里。

    用独立会话（`async_session_maker`）而不是请求级 session：调用方（登录钩子、
    将来的启动清理）通常正处于自己的事务里，清理是与该请求无关的维护动作。
    """
    if lifetime_seconds <= 0:
        # 与 services/auth.py 里 get_database_strategy 的防御同源：lifetime<=0 时
        # cutoff 会落在现在甚至未来，把**全部有效会话**当成过期删掉，比不清理危险得多。
        raise ValueError(
            f"cleanup_expired_access_tokens 的 lifetime_seconds 必须 > 0，当前为 {lifetime_seconds}："
            "0/负值会让服务端会话永不过期，按此清理会误删全部有效会话。"
        )

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=lifetime_seconds)
    async with async_session_maker() as session:
        result = await session.execute(delete(AccessToken).where(AccessToken.created_at < cutoff))
        await session.commit()
        return result.rowcount


async def delete_user_access_tokens(user_id: uuid.UUID) -> int:
    """删除某个用户的**全部**会话行，返回删除条数（改密 / 踢人 / 停用账号用）。

    只按 `user_id` 过滤：同一部署下改 A 的密码不能把 B 踢下线。
    """
    async with async_session_maker() as session:
        result = await session.execute(delete(AccessToken).where(AccessToken.user_id == user_id))
        await session.commit()
        return result.rowcount


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session


async def get_user_db(session: AsyncSession = Depends(get_async_session)):  # noqa: B008
    yield SQLAlchemyUserDatabase(session, User)


async def get_access_token_db(session: AsyncSession = Depends(get_async_session)):  # noqa: B008
    yield SQLAlchemyAccessTokenDatabase(session, AccessToken)
