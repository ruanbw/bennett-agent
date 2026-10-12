"""认证 HTTP 接口：注册 / 登录 / 登出 / 当前用户。

## 两个必须知道的接口约定

**1. 登录用表单编码，不是 JSON。** fastapi-users 的登录端点复用
`OAuth2PasswordRequestForm`，所以前端必须发
`application/x-www-form-urlencoded`，字段名是 `username`（填邮箱）与 `password`。
发 JSON 会直接 422，而不是给出「密码错误」这种有意义的提示。

**2. 凭据错误返回 400，不是 401。** detail 为 `LOGIN_BAD_CREDENTIALS`。
401 的含义是「访问受保护端点时没有有效登录态」，由 `current_active_user` 产生。
前端区分这两者才能给出正确提示（登录失败 vs 会话过期）。

## 为什么没有忘记密码 / 邮箱验证路由

这两个流程都要发邮件，当前未配置 SMTP。挂上路由却不发信，用户会以为重置邮件
已发出而一直等待，比没有该功能更糟。等接上邮件后再一并开启。
"""

from fastapi import APIRouter, Depends

from app.api.rate_limit import enforce_auth_rate_limit
from app.db import User
from app.schemas.user import UserCreate, UserRead
from app.services.auth import auth_backend, current_active_user, fastapi_users

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 注册按来源 IP 限流：防批量注册
router.include_router(
    fastapi_users.get_register_router(UserRead, UserCreate),
    dependencies=[Depends(enforce_auth_rate_limit)],
)


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
