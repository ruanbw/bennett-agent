"""认证 HTTP 接口：注册 / 登录 / 登出 / 当前用户。

登录必须发表单编码（`username` 填邮箱 + `password`），发 JSON 直接 422；凭据错误返回 400，401 专指没有有效登录态。
没有忘记密码 / 邮箱验证路由：未配 SMTP，挂上却不发信比没有更糟；超管密码丢失只能 `seed_admin.py --force-reset`。
"""

import logging

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi_users.exceptions import InvalidPasswordException, UserAlreadyExists

from app.api.rate_limit import enforce_auth_rate_limit, make_limiter, too_many
from app.config import settings
from app.db import User
from app.schemas.user import PasswordChangeRequest, UserCreate, UserRead, UserUpdate
from app.services.auth import (
    UserManager,
    auth_backend,
    current_active_user,
    fastapi_users,
    get_user_manager,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


#: 框架自动生成的三条路由的中文说明（fastapi-users 给的是英文 summary 且没有 description）。
#: 生成的路由没有能加装饰器的函数，只能按路径改写。
_FRAMEWORK_ROUTE_TEXT: dict[str, tuple[str, str]] = {
    "/login": (
        "登录（下发会话 cookie）",
        "请求体是**表单编码**（`application/x-www-form-urlencoded`，字段 `username` / `password`），"
        "不是 JSON：这是 OAuth2 密码流的标准形状。成功返回 204 并设置 HttpOnly 会话 cookie；"
        "失败返回 400（凭据错误）。同一来源 IP 有请求频率上限。",
    ),
    "/logout": (
        "登出（删除会话记录并清除 cookie）",
        "删除服务端 `access_token` 里的那一行，因此该会话立刻失效（不是「只让客户端忘掉」）。不要求请求体，幂等。",
    ),
    "/register": (
        "注册账号（新账号的角色是普通用户）",
        "邮箱 + 密码（至少 8 位）。新账号的角色固定为 **普通用户**（`user`）："
        "**没有任何请求参数能指定角色**，管理员身份只能由 `scripts/seed_admin.py` 播种"
        "或由超管在后台授予。同一来源 IP 有请求频率上限。",
    ),
}


def _localize_generated_routes(auth_router: APIRouter) -> None:
    """把框架生成的英文 summary/description 换成本项目的中文说明。"""
    for route in auth_router.routes:
        text = _FRAMEWORK_ROUTE_TEXT.get(getattr(route, "path", ""))
        if text is not None:
            route.summary, route.description = text


def _limit_login_only(auth_router: APIRouter) -> None:
    """给 login 路由挂上 IP 限流（它与 logout 生成在同一个 router 里，只能按路径挑）。

    不能两个都加：登出被限流会让用户连登出都做不了，而它也不是暴力破解的目标。
    """
    for route in auth_router.routes:
        if getattr(route, "path", None) == "/login":
            route.dependencies.append(Depends(enforce_auth_rate_limit))


# POST /api/auth/login、POST /api/auth/logout
_auth_router = fastapi_users.get_auth_router(auth_backend)
_limit_login_only(_auth_router)
_localize_generated_routes(_auth_router)
router.include_router(_auth_router)


async def enforce_registration_enabled() -> None:
    """注册开关：私有部署可用 `REGISTRATION_ENABLED=false` 关掉公开注册，改由种子脚本/管理员建号。"""
    if not settings.registration_enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="注册功能已关闭")


# POST /api/auth/register
#
# 不用框架自带的注册路由：它邮箱已存在时返回 400，等于给了邮箱枚举接口；
# 这里新建成功与邮箱已存在都返回 202 + 空 body，前端随后用同一组凭据登录。
@router.post(
    "/register",
    status_code=status.HTTP_202_ACCEPTED,
    summary="注册账号（新账号的角色是普通用户）",
    description=(
        "邮箱 + 密码（至少 8 位）。新账号的角色固定为**普通用户**（`user`）："
        "**没有任何请求参数能指定角色**，管理员身份只能由 `scripts/seed_admin.py` 播种"
        "或由超管在后台授予。同一来源 IP 有请求频率上限。"
        "无论邮箱是否已存在都返回 202，避免把注册接口变成邮箱枚举器。"
    ),
    dependencies=[Depends(enforce_auth_rate_limit), Depends(enforce_registration_enabled)],
)
async def register_account(
    payload: UserCreate,
    request: Request,
    user_manager: UserManager = Depends(get_user_manager),  # noqa: B008
) -> Response:
    try:
        await user_manager.create(payload, safe=True, request=request)
    except UserAlreadyExists:
        # 不区分「已注册」与「刚创建」：两者响应完全一致
        logger.info("注册请求命中已存在邮箱（返回 202，不泄露存在性）")
    except InvalidPasswordException as exc:
        # 密码策略不是身份信息，可以如实告诉用户
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"REGISTER_INVALID_PASSWORD: {exc.reason}",
        ) from exc
    return Response(status_code=status.HTTP_202_ACCEPTED)


#: 改密限流桶，按账号计数（登录是 IP 桶）：挡的是拿到会话后反复试当前密码与用 Argon2 打满 CPU。
_change_password_limiter: object | None = None


def get_change_password_limiter():  # noqa: ANN201
    """取改密限流器；配额/后端变化时重建（测试改 settings 后立即生效）。"""
    global _change_password_limiter
    _change_password_limiter = make_limiter(
        _change_password_limiter,
        max(settings.change_password_rate_limit_per_minute, 1),
    )
    return _change_password_limiter


def reset_change_password_limiter() -> None:
    """清空改密计数（测试用；与 `SlidingWindowLimiter.reset` 同口径）。"""
    global _change_password_limiter
    _change_password_limiter = None


async def enforce_change_password_rate_limit(
    user: User = Depends(current_active_user),  # noqa: B008
) -> None:
    """按账号限制改密尝试次数。

    声明了 `current_active_user`，所以即使配额配成 0 提前 return，登录要求依然成立。
    """
    if settings.change_password_rate_limit_per_minute <= 0:
        return
    allowed, retry_after = await get_change_password_limiter().acheck(f"user:{user.id}")
    if not allowed:
        raise too_many(retry_after, limit=get_change_password_limiter().limit)


@router.post(
    "/change-password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="修改当前账号密码",
    dependencies=[Depends(enforce_change_password_rate_limit)],
)
async def change_password(
    payload: PasswordChangeRequest,
    request: Request,
    user: User = Depends(current_active_user),  # noqa: B008
    user_manager: UserManager = Depends(get_user_manager),  # noqa: B008
) -> Response:
    """修改当前会话所属账号的密码，成功后该账号的全部会话（含本次）立即失效。

    必须验证当前密码：会话 cookie 可能来自别人临时用过的设备。新密码走与注册同一条校验。
    密码校验放进线程池：Argon2 约 65ms，跑在事件循环上等于让人拿改密端点打满 CPU。
    """
    # `verify_and_update` 顺带返回重算后的哈希，但本路由马上整行重写密码，不用在这落库。
    # Argon2 是 CPU 密集调用，必须放进 worker 线程，否则一次改密就能阻塞整个事件循环。
    valid, _ = await anyio.to_thread.run_sync(
        user_manager.password_helper.verify_and_update,
        payload.current_password,
        user.hashed_password,
    )
    if not valid:
        # 400 而非 401：401 会被前端的全局处理当成「会话过期」直接登出，而这里只是填错了当前密码。
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="CHANGE_PASSWORD_BAD_CREDENTIALS",
        )

    if payload.current_password == payload.new_password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="CHANGE_PASSWORD_SAME",
        )

    try:
        await user_manager.update(UserUpdate(password=payload.new_password), user, request=request)
    except InvalidPasswordException as exc:
        # 与注册保持同一个错误形状：前端把 reason 直接展示给用户
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"CHANGE_PASSWORD_INVALID: {exc.reason}",
        ) from exc

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=UserRead, summary="获取当前登录用户")
async def read_current_user(user: User = Depends(current_active_user)) -> User:  # noqa: B008
    """前端用它判断登录态并在启动时取回用户信息；未登录返回 401。

    `role` 每次请求都从库里读出，所以升降级后刷新页面即生效，不需要重新登录。
    """
    return user
