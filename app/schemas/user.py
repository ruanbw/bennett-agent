"""用户相关的请求/响应模型：复用 fastapi-users 基类，邮箱与密码校验交给框架。
`role` 只加在 `UserRead` 上，注册请求绝不接受它；注册走 `safe=True` 的 `create_update_dict()`，
它排除 is_active/is_superuser/is_verified，客户端无法自封管理员（回归锁 `tests/test_user_create.py`）。
"""

import uuid

from fastapi_users import schemas
from pydantic import BaseModel, Field

from app.roles import ROLE_USER


class UserRead(schemas.BaseUser[uuid.UUID]):
    """对外返回的用户信息。不含密码哈希。"""

    #: 角色字符串（`user` / `admin` / `superadmin`），见 `app/roles.py`；默认值让直接构造的调用点不必补字段。
    role: str = Field(default=ROLE_USER, description="角色：user / admin / superadmin")


class UserCreate(schemas.BaseUserCreate):
    """注册入参：`email` + `password`，刻意不含 `role`（见模块文档）。
    基类的 is_active / is_superuser / is_verified 由 `safe=True` 的 `create_update_dict()` 在落库前排除。
    """


class UserUpdate(schemas.BaseUserUpdate):
    """管理侧更新入参（目前只用于种子脚本重置密码），调用点只传 `password`。
    保留独立类型而非裸 dict：`UserManager.update()` 的 `validate_password` 才与线上登录走同一条路。
    """


class PasswordChangeRequest(BaseModel):
    """改密入参，不带 user_id：改的永远是当前会话对应的账号（接受它等于让「改别人密码」变成一个参数）。
    长度下限只在 `UserManager.validate_password` 里校验一次，这里只挡空串，避免两处规则分叉。
    """

    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)
