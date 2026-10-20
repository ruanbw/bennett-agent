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

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi_users.exceptions import InvalidPasswordException

from app.api.rate_limit import enforce_auth_rate_limit
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


@router.post(
    "/change-password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="修改当前账号密码",
)
async def change_password(
    payload: PasswordChangeRequest,
    request: Request,
    user: User = Depends(current_active_user),  # noqa: B008
    user_manager: UserManager = Depends(get_user_manager),  # noqa: B008
) -> Response:
    """修改**当前会话所属账号**的密码，成功后该账号的全部会话立即失效。

    三条规则：

    1. **必须验证当前密码**。会话 cookie 可能是别人临时用过的设备；只知道「已登录」
       不足以改密码，否则拾到一台未锁屏的机器就能永久接管账号。
    2. **新密码走与注册同一条校验**（`user_manager.update` → `validate_password`），
       不在路由里复制一份长度规则。
    3. **成功后踢掉所有会话**（`UserManager.on_after_update` 里的
       `delete_user_access_tokens`），包括发起请求的这一个：改密的语义就是「之前所有
       登录态都不再可信」。客户端随后会拿到 401 并回到登录页。

    不额外挂 IP 限流：这是已认证端点，且验证的是 8 位以上的真实密码——暴力破解不成立，
    而共用登录的限流桶反而会让「登录 + 改密」在 NAT 后面误伤。
    """
    # verify_and_update 是框架自带的校验入口：bcrypt 升级时它还会返回重算后的哈希，
    # 但本路由马上会整行重写密码，不需要在这里落库。
    valid, _ = user_manager.password_helper.verify_and_update(
        payload.current_password, user.hashed_password
    )
    if not valid:
        # 400 而不是 401：401 会被前端的全局处理当成「会话过期」直接登出，
        # 而这里只是表单里填错了当前密码，用户应当留在原地重试。
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
        await user_manager.update(
            UserUpdate(password=payload.new_password), user, request=request
        )
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

    返回体里的 `role` 决定了前端是否显示管理后台入口。也就是说，**升/降级后用户
    刷新页面就能看到效果**，不需要重新登录（角色存在库里，每次都由本次请求读出）。
    """
    return user
