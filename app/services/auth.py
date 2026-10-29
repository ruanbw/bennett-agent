"""认证装配：cookie 会话后端与「当前登录用户」依赖。

会话状态在 `access_token` 表而非 JWT，所以登出/改密/踢人立即生效；本地 http 必须 `cookie_secure=false`。
`AUTH_SECRET` 不参与会话（轮换不让登录失效），只用于重置/验证令牌与 `secret_box` 加密。
"""

import logging
import secrets
import uuid
from typing import Any

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi_users import BaseUserManager, FastAPIUsers, UUIDIDMixin, schemas
from fastapi_users.authentication import AuthenticationBackend, CookieTransport
from fastapi_users.authentication.strategy.db import AccessTokenDatabase, DatabaseStrategy
from fastapi_users.db import SQLAlchemyUserDatabase
from fastapi_users.exceptions import InvalidPasswordException
from sqlalchemy import func, select

from app.config import settings
from app.db import (
    AccessToken,
    HashedAccessTokenDatabase,
    User,
    async_session_maker,
    cleanup_expired_access_tokens,
    delete_user_access_tokens,
    get_access_token_db,
    get_user_db,
    hash_access_token,
)
from app.roles import ADMIN_LEVEL, ROLE_SUPERADMIN, Role

logger = logging.getLogger(__name__)

#: 密码最小长度：基类 `validate_password` 是空实现，不覆盖就能用 1 个字符注册。
MIN_PASSWORD_LENGTH = 8


def role_assignment(role: Role) -> dict[str, object]:
    """角色对应的列值（`role` 与两个派生布尔位），供 ORM 写入与 `admin.py` 的子查询 UPDATE 共用。

    管理员一并记为已验证：没有邮箱验证流程，is_verified 长期为 False 只是多一行无用提示。
    """
    values: dict[str, object] = {
        "role": role.value_str,
        "is_superuser": role >= ADMIN_LEVEL,
    }
    if role >= ADMIN_LEVEL:
        values["is_verified"] = True
    return values


def apply_role(user: User, role: Role) -> None:
    """唯一改动用户角色的 ORM 入口：同时写 `role` 与派生的 `is_superuser`。

    只改 `role` 会让内置 `current_user(superuser=True)` 与本项目判据给出相反结论；不提交事务，谁调用谁 commit。
    """
    for field, value in role_assignment(role).items():
        setattr(user, field, value)


async def count_superadmins() -> int:
    """当前 superadmin 数量。用于「不能降级最后一个超管」的前置检查。"""
    async with async_session_maker() as session:
        return await session.scalar(select(func.count()).select_from(User).where(User.role == ROLE_SUPERADMIN)) or 0


cookie_transport = CookieTransport(
    cookie_name=settings.auth_cookie_name,
    cookie_max_age=settings.session_lifetime_seconds,
    cookie_secure=settings.auth_cookie_secure,
    cookie_httponly=True,
    # 跨站部署（CORS 带凭据）必须 `none`，写死 `lax` 会让跨站 fetch 不回传 cookie（登录 204、下一个请求 401）。
    cookie_samesite=settings.resolved_cookie_samesite(),
)


class HashedDatabaseStrategy(DatabaseStrategy):
    """在 `DatabaseStrategy` 之上把入库值换成摘要：库被读时在线会话不能被直接回放。

    cookie 里仍是原文，读取由 `HashedAccessTokenDatabase` 先摘要后兼容明文。
    """

    async def write_token(self, user: User) -> str:
        token = secrets.token_urlsafe()
        await self.database.create({"token": hash_access_token(token), "user_id": user.id})
        return token


def get_database_strategy(
    access_token_db: AccessTokenDatabase[AccessToken] = Depends(get_access_token_db),  # noqa: B008
) -> DatabaseStrategy:
    """会话策略工厂：适配器持有请求级 session，不能做成模块级常量复用。"""
    lifetime = settings.session_lifetime_seconds
    if lifetime <= 0:
        # read_token 用 `if self.lifetime_seconds:` 判过期，0 是假值即永不过期；settings 已有 ge=60，这里再兜一道。
        raise ValueError(
            f"session_lifetime_seconds 必须 >=60，当前为 {lifetime}：0/负值会让服务端会话永不过期，请设 >=60。"
        )
    return HashedDatabaseStrategy(access_token_db, lifetime_seconds=lifetime)


auth_backend = AuthenticationBackend(
    name="cookie",
    transport=cookie_transport,
    get_strategy=get_database_strategy,
)


class UserManager(UUIDIDMixin, BaseUserManager[User, uuid.UUID]):
    """用户生命周期钩子；暂不接入邮件，邮箱验证与忘记密码不可自助。

    两个 token secret 是基类声明的类属性，框架不会校验非空，只在这两个流程被调用时才用到。
    """

    reset_password_token_secret = settings.auth_secret
    verification_token_secret = settings.auth_secret

    async def validate_password(self, password: str, user: schemas.UC | User) -> None:
        """密码强度校验：只设下限，强制大小写/符号只会把人逼向 `Password1!` 这类可预测模式。"""
        if len(password) < MIN_PASSWORD_LENGTH:
            raise InvalidPasswordException(reason=f"密码至少需要 {MIN_PASSWORD_LENGTH} 个字符")

    async def on_after_register(self, user: User, request: Request | None = None) -> None:
        """新用户注册后只记一条日志：注册开放，但建号不授予权限，提权只能走管理后台。"""
        logger.info("新用户注册: id=%s role=%s", user.id, user.role)

    async def on_after_login(
        self, user: User, request: Request | None = None, response: Response | None = None
    ) -> None:
        """登录成功后顺手清理过期会话行；失败只记警告（过期行堆积不该升级成「登录不可用」）。"""
        try:
            removed = await cleanup_expired_access_tokens(settings.session_lifetime_seconds)
        except Exception:  # noqa: BLE001 - 见 docstring：任何清理故障都必须被隔离
            logger.warning("清理过期会话失败（不影响本次登录）", exc_info=True)
            return

        if removed:
            logger.info("清理过期会话: 删除=%d", removed)

    async def on_after_update(self, user: User, update_dict: dict[str, Any], request: Request | None = None) -> None:
        """用户更新后让该用户的全部旧会话立即失效（框架更新成功后回调这里）。

        不吞异常：删失败却静默通过，等于泄露的旧会话继续可用。
        """
        removed = await delete_user_access_tokens(user.id)
        logger.info("用户更新，已失效其全部会话: id=%s 删除=%d", user.id, removed)


async def get_user_manager(user_db: SQLAlchemyUserDatabase = Depends(get_user_db)):  # noqa: B008
    yield UserManager(user_db)


fastapi_users = FastAPIUsers[User, uuid.UUID](get_user_manager, [auth_backend])


async def _read_user_from_token(token: str | None) -> User | None:
    """用独立短会话校验 cookie token，返回已加载的用户对象。

    不能复用 fastapi-users 的 `current_user`：它的请求级 session 要到响应结束才释放，SSE/长轮询会把连接池耗尽。
    必须复用 `DatabaseStrategy.read_token`（按 lifetime 过滤过期行），自己写 `token ==` 查询会让过期会话继续有效。
    """
    if not token:
        return None

    lifetime = settings.session_lifetime_seconds
    if lifetime <= 0:
        # 与 get_database_strategy 的防御同源：0 会让框架认为「不检查过期」
        raise ValueError(
            f"session_lifetime_seconds 必须 >=60，当前为 {lifetime}：0/负值会让服务端会话永不过期，请设 >=60。"
        )

    async with async_session_maker() as session:
        strategy = HashedDatabaseStrategy(
            HashedAccessTokenDatabase(session, AccessToken),
            lifetime_seconds=lifetime,
        )
        user_manager = UserManager(SQLAlchemyUserDatabase(session, User))
        return await strategy.read_token(token, user_manager)


async def current_active_user(request: Request) -> User:
    """端点认证依赖：从 HttpOnly cookie 解析当前用户，未登录/已停用 → 401。

    不声明数据库 `Depends`，否则流式与长轮询又会持有请求级连接（见 `_read_user_from_token`）。
    """
    user = await _read_user_from_token(request.cookies.get(settings.auth_cookie_name))
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


async def current_admin(user: User = Depends(current_active_user)) -> User:  # noqa: B008
    """管理后台依赖：`role >= admin` 才放行，否则 403。

    不用内置 `current_user(superuser=True)`：它读 `is_superuser`，漏同步一次就是静默越权。
    401 表示没有登录态、403 表示权限不够，前端靠这个区分「重新登录」与「找管理员」。
    """
    if user.role_level < ADMIN_LEVEL:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


async def current_superadmin(user: User = Depends(current_admin)) -> User:  # noqa: B008
    """最高一档：只有 superadmin 才能改别人的角色。"""
    if user.role_level < Role.SUPERADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要超级管理员权限（只有超级管理员可以调整角色）",
        )
    return user
