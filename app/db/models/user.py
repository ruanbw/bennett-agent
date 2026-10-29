"""用户与会话表：`role` 是唯一权限判据，会话 token 只存摘要。"""

import base64
import hashlib

from fastapi_users.db import SQLAlchemyBaseUserTableUUID
from fastapi_users_db_sqlalchemy.access_token import (
    SQLAlchemyAccessTokenDatabase,
    SQLAlchemyBaseAccessTokenTableUUID,
)
from sqlalchemy import String, delete
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.session_cache import _session_cache_delete, _session_cache_get, _session_cache_set
from app.roles import ROLE_USER, Role


class User(SQLAlchemyBaseUserTableUUID, Base):
    """用户表：fastapi-users 的标准列加上本项目的 `role`。

    `role` 是唯一权限判据（见 `app/roles.py`），只能由管理后台授予；`is_superuser` 只是它的
    派生镜像、由 `apply_role()` 单点写入，鉴权绝不读它，否则同一个问题会有两个答案。
    """

    #: 存成小写字符串而非 Enum：加档位只改 sql/init.sql，不必在 PG 维护类型。取值见 `app/roles.py`。
    role: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=ROLE_USER,
        server_default=ROLE_USER,
    )

    @property
    def role_level(self) -> Role:
        """解析后的角色等级。非法值时降级为 USER（见 `Role.parse`）。"""
        return Role.parse(self.role)


class AccessToken(SQLAlchemyBaseAccessTokenTableUUID, Base):
    """会话表：`token` / `user_id` / `created_at`；`user_id` 级联，删用户即清会话行。

    `token` 存的是 SHA-256 的 base64url 摘要而不是 cookie 原文，库被读也无法直接回放。
    """


def hash_access_token(token: str) -> str:
    """会话 token → 入库值（SHA-256 base64url，43 字符，正好卡进基表的 `String(43)`）。"""
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class HashedAccessTokenDatabase(SQLAlchemyAccessTokenDatabase):
    """写入摘要、按摘要查询，兼容历史明文行，并用 Redis 做 cache-aside 热缓存。

    库是权威：撤销先删库再删缓存，进程崩溃时的缓存残留由短 TTL（默认 60s）兜住，
    撤销的传播延迟因此有界。键是摘要而非原始 token，Redis 被读也拿不到可回放的令牌。
    """

    async def get_by_token(self, token: str, max_age=None):  # noqa: ANN001, ANN201
        hashed = hash_access_token(token)
        cached = await _session_cache_get(hashed)
        if cached is not None:
            return cached

        row = await super().get_by_token(hashed, max_age)
        if row is None:
            # 兼容升级前写入的明文行：无法提前哈希成键，故只回源不回填缓存。
            return await super().get_by_token(token, max_age)
        await _session_cache_set(hashed, row)
        return row

    async def delete(self, access_token) -> None:  # noqa: ANN001
        # 缓存命中的行是临时构造、不在任何会话里的实例，上游 `session.delete()` 对它
        # 会抛 InvalidRequestError（登出 500），所以统一按主键删。
        token = str(access_token.token)
        await self.session.execute(delete(self.access_token_table).where(self.access_token_table.token == token))
        await self.session.commit()
        # 先库后缓存：缓存删除失败时，TTL 到期后仍会回源到已删除的行。
        await _session_cache_delete(token)
