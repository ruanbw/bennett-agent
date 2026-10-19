"""用户相关的请求/响应模型。

直接复用 fastapi-users 的基类，而不是自己重写邮箱与密码校验规则——那套规则由框架
维护，抄一份迟早会与框架的校验漂移。

## role 为什么单独加在 UserRead 上

`role` 是本项目自己的列（fastapi-users 的基类没有），但它是前端决定「要不要显示
管理后台入口」的唯一依据，所以必须出现在 `/api/auth/me` 的响应里。

**放在 UserRead 而不是 UserCreate**：注册请求绝不接受 `role`。客户端能指定的任何
字段都是提权面，而建号只需要邮箱 + 密码。

## 安全说明

`BaseUserCreate` 里带着 `is_active` / `is_superuser` / `is_verified` 三个字段，但
注册路由调用的是 `user_manager.create(..., safe=True)`，而 `safe=True` 会走
`create_update_dict()`，该方法显式排除这三个字段（以及 `id`、`oauth_accounts`）。
因此**客户端无法通过注册接口自封管理员**。该结论已核对
`fastapi_users/schemas.py` 与 `fastapi_users/manager.py` 的实现，不是来自文档。

这条链路的回归锁是 `tests/test_user_create.py`：它在 schema 层断言 `role` 根本不是
`UserCreate` 的字段、`safe=True` 的 `create_update_dict()` 不携带那三个布尔位、以及
`UserRead.role` 默认为 `user`。

**HTTP 路由层已经没有覆盖**（注册/登录/cookie/会话失效那批用例随提交 2f48724 的
「移除全量/端到端测试层」决定一起删掉了，那是刻意的取舍，不是遗漏）。也就是说，
「路由确实传了 `safe=True`」这一环目前没有自动化用例盯着，只能靠
`app/api/auth.py` 保持原样：改动注册路由时必须回来确认这一点。
"""

import uuid

from fastapi_users import schemas
from pydantic import Field

from app.roles import ROLE_USER


class UserRead(schemas.BaseUser[uuid.UUID]):
    """对外返回的用户信息。不含密码哈希。"""

    #: 角色字符串（`user` / `admin` / `superadmin`），见 `app/roles.py`。
    #: 默认值让直接构造 UserRead 的调用点（测试、内部拼接）不用逐个补字段。
    role: str = Field(default=ROLE_USER, description="角色：user / admin / superadmin")


class UserCreate(schemas.BaseUserCreate):
    """注册入参：`email` + `password`。**刻意不含 `role`**（见模块文档）。

    基类自带的 `is_active` / `is_superuser` / `is_verified` 不是本类声明的，也不该由
    本类剔除——它们由 `safe=True` 的 `create_update_dict()` 在落库前排除（见模块文档），
    这样别人误用 `create_update_dict_superuser()` 时行为是显式的。
    """


class UserUpdate(schemas.BaseUserUpdate):
    """管理侧更新入参（目前只用于种子脚本重置密码）。

    `BaseUserUpdate` 的字段全部可选，`create_update_dict_superuser()` 不会被用来
    传入敏感字段——调用点只传 `password`。这里保留独立类型而不是裸 dict，是为了让
    `UserManager.update()` 的密码校验（`validate_password`）与线上登录走同一条路。
    """
