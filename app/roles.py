"""角色定义：三档 RBAC 的单一事实来源。

## 为什么要有这个模块

角色既是**数据**（`user.role` 列的取值）又是**判据**（依赖函数里的比较基准）。
把字符串散落在路由、schemas、种子脚本里，改档位名时会漏一处，而漏的那一处
通常表现为「某个管理员突然进不去后台」，不会在测试里亮红灯。

因此这里集中三件事：取值常量、等级排序、字符串解析。

## 三档的边界

| 角色 | 能做什么 |
|---|---|
| `user` | 只能对话。注册出来的账号默认就是这一档 |
| `admin` | 进管理后台：维护提供商 / 模型、封禁/解封**普通用户** |
| `superadmin` | 在 `admin` 之上再管两件事：**改动任何人的角色**（连 admin 也不行），以及越过「同级不可互封」的限制 |

改角色是 **superadmin 独占**的（`app/api/admin.py` 里那个端点挂的是
`current_superadmin`，admin 一律 403）。这里的措辞要准确：早期版的表写成「admin 能
给普通用户升 admin」，与实现相反，而本文件被当作单一事实来源——按错误的那行去
理解权限边界，会得出「随便一个 admin 就能造出新 admin」的错误结论。

分档的依据是「误操作代价」，不是「违规严重程度」（与 ADR-0005 同一取舍）：
模型提供商配置错一次会让全站没模型可用，而角色配错会让某人拿到或失去后台权限。
后者更难靠重试纠正，所以单独隔离到最高一档。

## 不变量

- **至少有一个 superadmin**。降级最后一个超管等于把管理后台永久锁死——服务里没有
  任何自助恢复路径（没有邮箱验证、没有忘记密码），只能改库。路由层强制这条。
- **没人能改自己的角色**。与「不能封禁自己」同源：把自己降级之后连撤销都做不到。
"""

from __future__ import annotations

from enum import IntEnum


class Role(IntEnum):
    """角色等级。**值的顺序就是权限顺序**，比较用 `>=`。"""

    USER = 0
    ADMIN = 1
    SUPERADMIN = 2

    @property
    def value_str(self) -> str:
        """落库与对外契约用的字符串形式（小写）。"""
        return self.name.lower()

    @classmethod
    def parse(cls, raw: str | None) -> Role:
        """解析字符串；无法识别时**降级为 USER**，而不是抛错。

        为什么宽容：`user.role` 是开放存储（VARCHAR，手工改库不会报错）。
        一个拼错的 'admn' 若让整个鉴权依赖抛 500，结果是「这个账号连同站点一起不可用」，
        比「他退回普通用户」严重得多。
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
