"""认证 HTTP 接口：注册 / 登录 / 登出 / 当前用户。

## 三个必须知道的接口约定

**1. 登录用表单编码，不是 JSON。** fastapi-users 的登录端点复用
`OAuth2PasswordRequestForm`，所以前端必须发
`application/x-www-form-urlencoded`，字段名是 `username`（填邮箱）与 `password`。
发 JSON 会直接 422，而不是给出「密码错误」这种有意义的提示。

**2. 凭据错误返回 400，不是 401。** detail 为 `LOGIN_BAD_CREDENTIALS`。
401 的含义是「访问受保护端点时没有有效登录态」，由 `current_active_user` 产生。
前端区分这两者才能给出正确提示（登录失败 vs 会话过期）。

**3. 注册要邀请码，未配置就关闭公开注册。** 这里不用 fastapi-users 自带的注册路由，
原因见下面的模块说明与 `register` 的 docstring：邮箱字符串在无验证环节的项目里不能
证明归属，靠「注册即提权」引导超管等于把管理后台交给第一个抢注白名单邮箱的人。

## 为什么没有忘记密码 / 邮箱验证路由

这两个流程都要发邮件，当前未配置 SMTP。挂上路由却不发信，用户会以为重置邮件
已发出而一直等待，比没有该功能更糟。等接上邮件后再一并开启。
"""

import hmac
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi_users import exceptions
from fastapi_users.manager import BaseUserManager
from fastapi_users.router.common import ErrorCode
from pydantic import BaseModel, EmailStr, Field

from app.api.rate_limit import enforce_auth_rate_limit
from app.config import settings
from app.db import User
from app.schemas.user import UserCreate, UserRead
from app.services.auth import (
    auth_backend,
    current_active_user,
    fastapi_users,
    get_user_manager,
    is_configured_superuser,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

#: 未配置邀请码时的拒绝文案。刻意与「邀请码错误」用同一状态码、不同文案：用户只需
#: 知道自己该找谁，不需要知道服务端有没有配过邀请码。
REGISTRATION_CLOSED_DETAIL = "注册已关闭，请联系管理员"

#: 邀请码不匹配的文案。**不回显正确值**，也不回显用户提交的值。
INVITE_CODE_INVALID_DETAIL = "邀请码不正确"

# 注册请求体的字段上限。注册是**未认证**入口，请求体完全不可信，这里挡住超大载荷；
# 密码长度规则仍交给 `UserManager.validate_password`，以保持「密码太短」原有的
# 400 + `REGISTER_INVALID_PASSWORD` 语义（放进 pydantic 会变成 422）。
MAX_EMAIL_CHARS = 320
MAX_PASSWORD_CHARS = 256
MAX_INVITE_CODE_CHARS = 256


class RegisterRequest(BaseModel):
    """注册入参：`email` + `password` + `invite_code`。

    刻意不复用 `UserCreate`（即便是子类也不行）：`create_update_dict()` 会把模式里的
    **所有**字段原样交给 `User(**...)`，多出来的 `invite_code` 会让建号直接 TypeError。
    因此这里独立定义，再由路由组装出 `UserCreate` 交给 `user_manager.create()`。
    """

    email: EmailStr = Field(..., max_length=MAX_EMAIL_CHARS)
    password: str = Field(..., max_length=MAX_PASSWORD_CHARS)
    invite_code: str = Field(default="", max_length=MAX_INVITE_CODE_CHARS)


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(enforce_auth_rate_limit)],
    summary="邀请码注册",
)
async def register(
    request: Request,
    payload: RegisterRequest,
    user_manager: BaseUserManager[User, uuid.UUID] = Depends(get_user_manager),  # noqa: B008
) -> User:
    """邀请码注册；未配置邀请码时一律 403。

    与 fastapi-users 默认注册路由的差异有两点：

    1. 多一道邀请码校验：邀请码是「谁被允许建号」的凭据，部署者掌握，客户端不可猜。
       比较用 `hmac.compare_digest`（常数时间），失败文案不透露正确值。
    2. 白名单邮箱的提权在这里显式完成。原来它挂在 `on_after_register` 上，任何人都
       可以抢先注册白名单邮箱直接成为超管（项目没有邮箱验证，邮箱字符串证明不了归属）。
       现在必须**邀请码正确 + 邮箱在白名单**才提权。

    邮箱重复仍是 400 + `REGISTER_USER_ALREADY_EXISTS`，与默认路由一致。
    """
    configured_code = settings.registration_invite_code
    if not configured_code:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=REGISTRATION_CLOSED_DETAIL)

    # 编码成 bytes 再比：compare_digest 对含非 ASCII 的 str 会抛 TypeError
    if not hmac.compare_digest(payload.invite_code.encode(), configured_code.encode()):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=INVITE_CODE_INVALID_DETAIL)

    user_create = UserCreate(email=payload.email, password=payload.password)
    try:
        created = await user_manager.create(user_create, safe=True, request=request)
    except exceptions.UserAlreadyExists:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorCode.REGISTER_USER_ALREADY_EXISTS,
        ) from None
    except exceptions.InvalidPasswordException as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": ErrorCode.REGISTER_INVALID_PASSWORD, "reason": exc.reason},
        ) from exc

    if is_configured_superuser(created.email):
        created = await user_manager.user_db.update(created, {"is_superuser": True, "is_verified": True})
        logger.info("白名单账号经邀请码注册，已提升为超级管理员: id=%s", created.id)

    return created


def _limit_login_only(auth_router: APIRouter) -> None:
    """给 login 路由挂上 IP 限流。

    fastapi-users 把 login / logout 生成在同一个 router 里，没有可以直接加装饰器的
    函数，只能按路径挑。

    **不能两个都加**：给登出也限流会造出一个糟糕的失败模式——用户被限流后连登出都
    做不了；而登出本身已经要求登录，也不是暴力破解的目标。
    """
    for route in auth_router.routes:
        if getattr(route, "path", None) == "/login":
            route.dependencies.append(Depends(enforce_auth_rate_limit))


# POST /api/auth/login、POST /api/auth/logout
_auth_router = fastapi_users.get_auth_router(auth_backend)
_limit_login_only(_auth_router)
router.include_router(_auth_router)


@router.get("/me", response_model=UserRead, summary="获取当前登录用户")
async def read_current_user(user: User = Depends(current_active_user)) -> User:  # noqa: B008
    """前端用它判断登录态并在启动时取回用户信息；未登录返回 401。"""
    return user
