"""角色定义：三档 RBAC 的单一事实来源（取值常量、等级排序、字符串解析）。

`user` 只能对话；`admin` 管提供商/模型并封禁普通用户；`superadmin` 独占「改任何人角色」与
「越过同级不可互封」。不变量：至少留一个 superadmin，且没人能改自己的角色。
"""

from __future__ import annotations

from enum import IntEnum


class Role(IntEnum):
    """角色等级。值的顺序就是权限顺序，比较用 `>=`。"""

    USER = 0
    ADMIN = 1
    SUPERADMIN = 2

    @property
    def value_str(self) -> str:
        """落库与对外契约用的字符串形式（小写）。"""
        return self.name.lower()

    @classmethod
    def parse(cls, raw: str | None) -> Role:
        """解析字符串；无法识别时降级为 USER，而不是抛错。

        `user.role` 是开放存储（手工改库不会报错）：一个拼错的 'admn' 若让鉴权依赖抛 500，
        结果是「这个账号连同站点一起不可用」，比「他退回普通用户」严重得多。
        """
        if not raw:
            return cls.USER
        cleaned = raw.strip().lower()
        aliases = {"superadmin": cls.SUPERADMIN, "admin": cls.ADMIN, "user": cls.USER}
        return aliases.get(cleaned, cls.USER)


#: 对外/入库用的字符串常量。路由与 schemas 引用这里，不写字面量。
ROLE_USER = Role.USER.value_str
ROLE_ADMIN = Role.ADMIN.value_str
ROLE_SUPERADMIN = Role.SUPERADMIN.value_str

#: 所有合法取值，用于 pydantic 的 `Literal` 与前端契约对齐。
ALL_ROLES: tuple[str, ...] = (ROLE_USER, ROLE_ADMIN, ROLE_SUPERADMIN)

#: 能进管理后台的最低等级。
ADMIN_LEVEL = Role.ADMIN
