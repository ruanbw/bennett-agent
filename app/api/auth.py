"""认证 HTTP 接口：注册 / 登录 / 登出 / 当前用户。

## 三个必须知道的接口约定

**1. 登录用表单编码，不是 JSON。** fastapi-users 的登录端点复用
`OAuth2PasswordRequestForm`，所以前端必须发
`application/x-www-form-urlencoded`，字段名是 `username`（填邮箱）与 `password`。
发 JSON 会直接 422，而不是给出「密码错误」这种有意义的提示。

**2. 凭据错误返回 400，不是 401。** detail 为 `LOGIN_BAD_CREDENTIALS`。
401 的含义是「访问受保护端点时没有有效登录态」，由 `current_active_user` 产生。
前端区分这两者才能给出正确提示（登录失败 vs 会话过期）。

**3. 注册完全开放，但注册出来的账号没有任何特权。** 直接用 fastapi-users 的注册
路由：注册只做「建一个 role=user 的账号」，不涉及提权。
初始超管由 `scripts/seed_admin.py` 播种（管理员是**部署者已知的数据**，不靠公开接口
抢注），其余账号的提权一律走管理后台的角色接口（见 `app/api/admin.py`）。

## 为什么没有忘记密码 / 邮箱验证路由

这两个流程都要发邮件，当前未配置 SMTP。挂上路由却不发信，用户会以为重置邮件
已发出而一直等待，比没有该功能更糟。等接上邮件后再一并开启。
**这也意味着超管密码一旦丢失没有自助恢复路径**——用 `seed_admin.py --force-reset`
改回即可，那是刻意保留的运维通道。
"""

import logging

from fastapi import APIRouter, Depends

from app.api.rate_limit import enforce_auth_rate_limit
from app.db import User
from app.schemas.user import UserCreate, UserRead
from app.services.auth import auth_backend, current_active_user, fastapi_users

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])


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

# POST /api/auth/register —— 框架自带的注册路由：建号后 role 取模型默认值（user）
_register_router = fastapi_users.get_register_router(UserRead, UserCreate)
for _route in _register_router.routes:
    if getattr(_route, "path", None) == "/register":
        # 注册是**未认证**入口，同样要按 IP 挡批量建号
        _route.dependencies.append(Depends(enforce_auth_rate_limit))
router.include_router(_register_router)


@router.get("/me", response_model=UserRead, summary="获取当前登录用户")
async def read_current_user(user: User = Depends(current_active_user)) -> User:  # noqa: B008
    """前端用它判断登录态并在启动时取回用户信息；未登录返回 401。

    返回体里的 `role` 决定了前端是否显示管理后台入口。也就是说，**升/降级后用户
    刷新页面就能看到效果**，不需要重新登录（角色存在库里，每次都由本次请求读出）。
    """
    return user
