"""认证装配：cookie 会话后端与「当前登录用户」依赖。

## 为什么不用 JWT

JWT 把状态放在令牌内部，服务端无法单方面作废——登出只是让客户端删掉本地副本，
令牌在过期前依然有效。这里的组合是：

- 传输层 `CookieTransport`：令牌放 HttpOnly cookie，JS 读不到，XSS 偷不走。
- 策略层 `DatabaseStrategy`：令牌只是一串随机值，真正的状态在 `access_token` 表里。

于是登出 = 删一行，改密 = 删该用户所有行，踢人 = 删指定行，**全部立即生效**。
这正是 JWT 做不到的部分，也是本项目选会话而非令牌的原因。

## cookie 与跨域

`SameSite=Lax` 配前端同源（Vite 把 `/api` 代理到本服务），所以不需要
`allow_credentials=True`，`app/server.py` 的 CORS 配置保持原样即可。

`cookie_secure` 来自 `settings.auth_cookie_secure`：**本地 http 下必须为 false**，
否则浏览器不会回传 cookie（框架该参数默认是 true，是个常见的坑）。

## AUTH_SECRET 的真实作用范围

`settings.auth_secret` **不参与会话**：cookie 里放的是不透明服务端 token，既不签名，
轮换它也不会让任何登录态失效（要让会话失效请删 `access_token` 行）。它目前只喂给
`UserManager.reset_password_token_secret` / `verification_token_secret`，等将来接入
邮件流程（重置密码、邮箱验证）时才真正生效。
"""

import logging
import uuid
from typing import Any

from fastapi import Depends, Request, Response
from fastapi_users import BaseUserManager, FastAPIUsers, UUIDIDMixin, schemas
from fastapi_users.authentication import AuthenticationBackend, CookieTransport
from fastapi_users.authentication.strategy.db import AccessTokenDatabase, DatabaseStrategy
from fastapi_users.db import SQLAlchemyUserDatabase
from fastapi_users.exceptions import InvalidPasswordException
from sqlalchemy import func, update

from app.config import settings
from app.db import (
    AccessToken,
    User,
    async_session_maker,
    cleanup_expired_access_tokens,
    delete_user_access_tokens,
    get_access_token_db,
    get_user_db,
)

logger = logging.getLogger(__name__)

#: 密码最小长度。
#:
#: 为什么需要显式定一个：基类的 `validate_password` 是**空实现**（源码里只有
#: "You should overload this method" 一句注释），不覆盖就能用 1 个字符注册成功
#: （已实测 201）。本项目没有邮箱验证、忘记密码也走不了自助流程，密码是唯一凭据，
#: 因此设下限是有意义的，不是形式主义。
MIN_PASSWORD_LENGTH = 8


def is_configured_superuser(email: str | None) -> bool:
    """邮箱是否在 `SUPERUSER_EMAILS` 白名单里（大小写不敏感）。"""
    if not email:
        return False
    return email.strip().lower() in settings.get_superuser_emails()


async def promote_configured_superusers() -> int:
    """启动时把白名单邮箱对应的账号提升为超级管理员，返回变更行数。

    管理后台没有「把自己设为管理员」的接口——那等于把提权入口暴露给任何登录用户。
    引导只能来自部署者掌握的环境变量。每次启动重跑一遍：运维新增邮箱后重启即生效，
    也不需要手工改库。幂等（已标记的不会计入行数）。
    """
    emails = settings.get_superuser_emails()
    if not emails:
        return 0

    async with async_session_maker() as session:
        result = await session.execute(
            update(User)
            .where(func.lower(User.email).in_(emails), User.is_superuser.is_(False))
            .values(is_superuser=True, is_verified=True)
        )
        await session.commit()
        changed = result.rowcount or 0

    if changed:
        logger.info("已提升 %d 个账号为超级管理员", changed)
    return changed


cookie_transport = CookieTransport(
    cookie_name=settings.auth_cookie_name,
    cookie_max_age=settings.session_lifetime_seconds,
    cookie_secure=settings.auth_cookie_secure,
    cookie_httponly=True,
    cookie_samesite="lax",
)


def get_database_strategy(
    access_token_db: AccessTokenDatabase[AccessToken] = Depends(get_access_token_db),  # noqa: B008
) -> DatabaseStrategy:
    """会话策略。

    必须写成函数而不是模块级常量：`DatabaseStrategy` 需要注入数据库适配器，而适配器
    持有每个请求独立的 SQLAlchemy session，不能跨请求复用。
    """
    lifetime = settings.session_lifetime_seconds
    if lifetime <= 0:
        # 防御纵深：fastapi-users 的 read_token 用 `if self.lifetime_seconds:` 判断是否
        # 检查过期（0 是假值 → 永不过期），而 settings 目前没有拒绝 0/负值的校验。
        # 这里补一道，任何绕过 settings 校验的调用路径都拿不到永不过期的策略。
        raise ValueError(
            f"session_lifetime_seconds 必须 >=60，当前为 {lifetime}：0/负值会让服务端会话永不过期，请设 >=60。"
        )
    return DatabaseStrategy(access_token_db, lifetime_seconds=lifetime)


auth_backend = AuthenticationBackend(
    name="cookie",
    transport=cookie_transport,
    get_strategy=get_database_strategy,
)


class UserManager(UUIDIDMixin, BaseUserManager[User, uuid.UUID]):
    """用户生命周期钩子。

    **暂不接入邮件**：邮箱验证与忘记密码都需要 SMTP，当前未配置，因此这两个流程
    不可自助。

    `reset_password_token_secret` / `verification_token_secret` 是基类声明的类属性，
    框架**不会**在导入时校验其非空，只在这两个流程真正被调用时才用到。这里一并设置，
    避免将来接上邮件流程时拿着空密钥签发令牌。
    """

    reset_password_token_secret = settings.auth_secret
    verification_token_secret = settings.auth_secret

    async def validate_password(self, password: str, user: schemas.UC | User) -> None:
        """密码强度校验（基类默认不校验，见 MIN_PASSWORD_LENGTH 的说明）。

        只设下限而不加复杂度规则：长度是唯一与真实熵相关的约束，强制大小写/符号
        只会把人逼向 `Password1!` 这种可预测模式。
        """
        if len(password) < MIN_PASSWORD_LENGTH:
            raise InvalidPasswordException(reason=f"密码至少需要 {MIN_PASSWORD_LENGTH} 个字符")

    async def on_after_register(self, user: User, request: Request | None = None) -> None:
        """新用户注册后：白名单邮箱立即提升为管理员。

        只靠启动提升不够：管理员账号往往是在服务启动之后才注册的。`security` 保证
        注册接口不能自封管理员（`safe=True` 排除了该字段），这里的提升只认部署者
        写在环境变量里的邮箱，不认请求体。
        """
        logger.info("新用户注册: id=%s", user.id)
        if is_configured_superuser(user.email):
            await self.user_db.update(user, {"is_superuser": True, "is_verified": True})
            logger.info("白名单账号注册，已提升为超级管理员: id=%s", user.id)

    async def on_after_login(
        self, user: User, request: Request | None = None, response: Response | None = None
    ) -> None:
        """登录成功后顺手清理过期会话行（best-effort，失败不影响登录）。

        登录是天然低频、幂等的维护时刻，不需要为清理单独引入定时任务。
        清理失败（例如库被锁）只记警告：过期行的堆积不该升级成「登录不可用」。
        """
        try:
            removed = await cleanup_expired_access_tokens(settings.session_lifetime_seconds)
        except Exception:  # noqa: BLE001 - 见 docstring：任何清理故障都必须被隔离
            logger.warning("清理过期会话失败（不影响本次登录）", exc_info=True)
            return

        if removed:
            logger.info("清理过期会话: 删除=%d", removed)

    async def on_after_update(self, user: User, update_dict: dict[str, Any], request: Request | None = None) -> None:
        """用户更新后让该用户的全部旧会话立即失效。

        重置密码/改密路由未来挂载时自动生效（只要它们经 `user_manager.update()` 落库）：
        框架更新成功后回调这里，所以「改密 = 删该用户所有行」不必在路由里重复实现。
        这里**不吞异常**——删失败却静默通过，等于泄露的旧会话继续可用。
        """
        removed = await delete_user_access_tokens(user.id)
        logger.info("用户更新，已失效其全部会话: id=%s 删除=%d", user.id, removed)


async def get_user_manager(user_db: SQLAlchemyUserDatabase = Depends(get_user_db)):  # noqa: B008
    yield UserManager(user_db)


fastapi_users = FastAPIUsers[User, uuid.UUID](get_user_manager, [auth_backend])

#: 端点网关依赖：未登录或账号已停用 → 401。
current_active_user = fastapi_users.current_user(active=True)

#: 管理后台依赖：未登录/停用 → 401，登录但不是超级管理员 → 403。
#: `active=True` 同时覆盖了「封禁后即使 token 没被删也进不来」这一层。
current_superuser = fastapi_users.current_user(active=True, superuser=True)
