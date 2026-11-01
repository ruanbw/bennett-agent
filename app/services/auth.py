"""认证装配：cookie 会话后端与「当前登录用户」依赖。

会话状态在 `access_token` 表而非 JWT，所以登出/改密/踢人立即生效；本地 http 必须 `cookie_secure=false`。
`AUTH_SECRET` 不参与会话（轮换不让登录失效），只用于重置/验证令牌与 `secret_box` 加密。
"""

import hashlib
import logging
import secrets
import uuid
from typing import Any

import anyio
from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from fastapi_users import BaseUserManager, FastAPIUsers, UUIDIDMixin
from fastapi_users.authentication import AuthenticationBackend, CookieTransport
from fastapi_users.authentication.strategy.db import AccessTokenDatabase, DatabaseStrategy
from fastapi_users.db import SQLAlchemyUserDatabase
from fastapi_users.exceptions import InvalidPasswordException, UserAlreadyExists, UserNotExists
from sqlalchemy import func, select

from app.api.rate_limit import RedisSlidingWindowLimiter, SlidingWindowLimiter, too_many
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
from app.schemas.user import UserCreate
from app.services.metrics import metrics

logger = logging.getLogger(__name__)

#: 密码最小长度：基类 `validate_password` 是空实现，不覆盖就能用 1 个字符注册。
#: 12 是 ASVS 2.1.1 的下限；只限长度、不强制字符种类——组合规则只会把人逼向 `Password1!`。
MIN_PASSWORD_LENGTH = 12

#: 密码最大长度（ASVS 2.1.2）。改密入参 `PasswordChangeRequest.max_length` 也是 128：两侧
#: 不一致时，用超长口令注册的账号永远改不了密码（当前密码被 422 拦下），且超长输入是对
#: Argon2 的免费放大量。
MAX_PASSWORD_LENGTH = 128

#: 账号维度登录失败计数的窗口（秒）。ASVS 2.2.1 的口径是「单账号每小时失败次数」，所以窗口
#: 必须是一小时；`app/api/rate_limit.py:make_limiter` 的窗口写死 60 秒（那是按 IP 的快速闸门），
#: 在账号维度上表达不了每小时配额。
ACCOUNT_FAILURE_WINDOW_SECONDS = 3600.0

#: 登录失败计数器。标签 `reason` 只取三个值（`unknown_email` / `wrong_password` / `account_locked`）：
#: 高基数标签会把指标自己变成内存泄漏（见 `app/services/metrics.py:MAX_SERIES_PER_METRIC`）。
AUTH_LOGIN_FAILURES_TOTAL = "auth_login_failures_total"


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


#: 账号维度的失败计数桶（惰性创建，配额/后端变化时重建）。
_account_failure_limiter: SlidingWindowLimiter | RedisSlidingWindowLimiter | None = None


def get_account_failure_limiter() -> SlidingWindowLimiter | RedisSlidingWindowLimiter:
    """取账号失败计数桶；配额或存储后端变化时重建（测试改 settings 后立即生效）。

    不经过 `rate_limit.make_limiter`：那个工厂把窗口写死成 60 秒，而这里要的是每小时配额。
    后端语义与它保持一致（redis 跨实例共享、memory 仅供单 worker/测试）。
    """
    global _account_failure_limiter
    limit = max(settings.account_login_failure_limit_per_hour, 1)
    if settings.rate_limit_backend == "redis":
        if isinstance(_account_failure_limiter, RedisSlidingWindowLimiter) and _account_failure_limiter.limit == limit:
            return _account_failure_limiter
        _account_failure_limiter = RedisSlidingWindowLimiter(limit=limit, window_seconds=ACCOUNT_FAILURE_WINDOW_SECONDS)
    else:
        if isinstance(_account_failure_limiter, SlidingWindowLimiter) and _account_failure_limiter.limit == limit:
            return _account_failure_limiter
        _account_failure_limiter = SlidingWindowLimiter(limit=limit, window_seconds=ACCOUNT_FAILURE_WINDOW_SECONDS)
    return _account_failure_limiter


def reset_account_failure_limiter() -> None:
    """丢弃缓存的计数桶（测试用）。不整片清 Redis 键：那会顺带解掉别的账号的锁。"""
    global _account_failure_limiter
    _account_failure_limiter = None


class UserManager(UUIDIDMixin, BaseUserManager[User, uuid.UUID]):
    """用户生命周期钩子；暂不接入邮件，邮箱验证与忘记密码不可自助。

    两个 token secret 是基类声明的类属性，框架不会校验非空，只在这两个流程被调用时才用到。
    """

    reset_password_token_secret = settings.auth_secret
    verification_token_secret = settings.auth_secret

    async def validate_password(self, password: str, user: UserCreate | User) -> None:
        """密码强度校验：只设长度上下限，强制大小写/符号只会把人逼向 `Password1!` 这类可预测模式。

        上下限都在这里判一次：注册、改密、种子脚本共用同一个判据，两处规则分叉的后果是
        「能注册但改不掉」（见 `MAX_PASSWORD_LENGTH` 的注释）。
        """
        if len(password) < MIN_PASSWORD_LENGTH:
            raise InvalidPasswordException(reason=f"密码至少需要 {MIN_PASSWORD_LENGTH} 个字符")
        if len(password) > MAX_PASSWORD_LENGTH:
            raise InvalidPasswordException(reason=f"密码最多 {MAX_PASSWORD_LENGTH} 个字符")

    async def create(
        self,
        user_create: UserCreate,
        safe: bool = False,
        request: Request | None = None,
    ) -> User:
        """覆盖框架实现：只把 `password_helper.hash` 挪进 worker 线程，其余步骤逐句保持上游。

        框架没有「哈希钩子」，改不动 `_update`/`authenticate` 里的同步调用，所以这里必须复制
        `BaseUserManager.create` 的主体（validate → 查重 → 落库 → `on_after_register`）。
        升级 fastapi-users 时要对照上游复核：漏掉新增步骤的症状是注册路径静默少一道校验。
        为什么必须挪：注册是**未认证**可达的，一次 Argon2 约 65ms 且占几十 MiB，同步跑在
        事件循环线程上等于把整个进程（含 SSE 与长轮询）交给任何匿名请求去卡。
        """
        await self.validate_password(user_create.password, user_create)

        existing_user = await self.user_db.get_by_email(user_create.email)
        if existing_user is not None:
            raise UserAlreadyExists()

        user_dict = user_create.create_update_dict() if safe else user_create.create_update_dict_superuser()
        password = user_dict.pop("password")
        user_dict["hashed_password"] = await anyio.to_thread.run_sync(self.password_helper.hash, password)

        created_user = await self.user_db.create(user_dict)

        await self.on_after_register(created_user, request)

        return created_user

    async def authenticate(self, credentials: OAuth2PasswordRequestForm) -> User | None:
        """覆盖框架实现：Argon2 挪进 worker 线程，失败分支留下账号维度的痕迹。

        主体与 `BaseUserManager.authenticate` 逐句等价（含「邮箱不存在也要跑一次假哈希」这条
        时间旁路缓解），区别是两处哈希都走 `anyio.to_thread`：登录同样**未认证**可达，一次
        尝试阻塞事件循环约 65ms，多 IP 并发几十个就能把进程的内存与 CPU 一起打满。
        rehash-on-login 仍按上游写法直接写 `user_db`（不经过 `update`，避免连带删会话）。
        """
        try:
            user = await self.get_by_email(credentials.username)
        except UserNotExists:
            # 账号不存在也必须跑一次哈希：否则「没这个邮箱」会立刻返回，那个稳定的几十毫秒差
            # 就是远程可用的邮箱枚举器。这条缓解同样不能留在事件循环线程上。
            await anyio.to_thread.run_sync(self.password_helper.hash, credentials.password)
            self._log_login_failure(credentials.username, reason="unknown_email")
            return None

        verified, updated_password_hash = await anyio.to_thread.run_sync(
            self.password_helper.verify_and_update,
            credentials.password,
            user.hashed_password,
        )
        if not verified:
            self._log_login_failure(credentials.username, reason="wrong_password")
            await self._enforce_account_failure_limit(user)
            return None

        if updated_password_hash is not None:
            await self.user_db.update(user, {"hashed_password": updated_password_hash})

        return user

    @staticmethod
    def _email_digest(email: str) -> str:
        """邮箱摘要（8 位十六进制）。归一化大小写，否则同一个账号会散成两条日志线索。"""
        return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:8]

    @classmethod
    def _log_login_failure(cls, email: str, *, reason: str) -> None:
        """登录失败留痕：日志只写邮箱摘要，指标按 `reason` 分桶。

        不落明文邮箱：日志会被轮转、抓取、贴进工单，明文邮箱等于把用户清单抄一份出去；
        摘要足够把同一账号的连续失败串起来，又不足以反推地址。
        """
        logger.warning("登录失败: email_hash=%s reason=%s", cls._email_digest(email), reason)
        metrics.increment(AUTH_LOGIN_FAILURES_TOTAL, reason=reason)

    async def _enforce_account_failure_limit(self, user: User) -> None:
        """账号维度的失败上限：超了就让这次失败变成 429（ASVS 2.2.1）。

        按 IP 的桶（`app/api/rate_limit.py:enforce_auth_rate_limit`）换个出口即绕过，分布式
        凭据填充只能按账号拦；默认 20/小时，比 ASVS 的 100/小时更严。
        只在**失败**分支计数，所以正确密码永远能登录，被锁的只是「继续猜」——合法用户不会被
        锁在门外，这也是这里不需要「成功即清零」的原因（限流器没有单键删除接口，整片
        `reset()` 反而给了攻击者「登录自己的账号来解锁目标账号」的能力）。
        """
        limit = settings.account_login_failure_limit_per_hour
        if limit <= 0:
            return
        limiter = get_account_failure_limiter()
        allowed, retry_after = await limiter.acheck(f"account:{user.id}")
        if allowed:
            return
        # 这一条是刚那次失败尝试的**附加标记**，不是第二次尝试：看板按 reason 分开看，
        # 把三个 reason 相加不等于失败次数。
        self._log_login_failure(user.email, reason="account_locked")
        raise too_many(retry_after, limit=limiter.limit)

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
