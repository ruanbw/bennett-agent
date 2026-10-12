"""用户相关的请求/响应模型。

直接复用 fastapi-users 的基类，而不是自己重写邮箱与密码校验规则——那套规则由框架
维护，抄一份迟早会与框架的校验漂移。

**安全说明**：`BaseUserCreate` 里带着 `is_active` / `is_superuser` / `is_verified`
三个字段，但注册路由调用的是 `user_manager.create(..., safe=True)`，而 `safe=True`
会走 `create_update_dict()`，该方法显式排除这三个字段（以及 `id`、`oauth_accounts`）。
因此**客户端无法通过注册接口自封管理员**。该结论已核对
`fastapi_users/schemas.py` 与 `fastapi_users/manager.py` 的实现，不是来自文档。
"""

import uuid

from fastapi_users import schemas


class UserRead(schemas.BaseUser[uuid.UUID]):
    """对外返回的用户信息。不含密码哈希。"""


class UserCreate(schemas.BaseUserCreate):
    """注册入参：`email` + `password`。"""
