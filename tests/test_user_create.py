"""注册入参的提权面：schema 层锁住「客户端不能自封管理员」。

HTTP 层用例（提交 2f48724 移除了全量/端到端测试层）已经不在了，所以这条链路上唯一
的自动化保护在这里：`role` 不是 `UserCreate` 的字段，且它从基类继承来的三个布尔位
在 `safe=True` 的 `create_update_dict()` 里被排除。两点都测，是因为它们分别对应
「客户端能声明什么」与「声明了也进不了库」两个不同环节——只测后者的话，有人把
`role` 加进 `UserCreate` 并顺手改一条 `create_update_dict` 的白名单，仍然会漏。
"""

import uuid

from app.roles import ROLE_USER
from app.schemas.user import UserCreate, UserRead


def test_user_create_declares_only_the_expected_fields():
    """字段集合本身就是契约：`role` 不在里面，能塞进来的只有基类那三个布尔位。

    逐个断言 `role`「不存在」不够——提权面还会因为新增字段而扩大。固定整个集合，
    加字段的人就必须来这行确认新字段是否也该进 `create_update_dict()` 的排除表。
    """
    assert set(UserCreate.model_fields) == {"email", "password", "is_active", "is_superuser", "is_verified"}


def test_role_sent_by_the_client_is_not_carried_on_the_model():
    """请求体里带 `role` 时，它不会变成模型属性，也不会出现在任何落库字典里。"""
    create = UserCreate.model_validate({"email": "a@example.com", "password": "password123", "role": "superadmin"})

    assert not hasattr(create, "role")
    assert "role" not in create.model_dump()
    assert "role" not in create.create_update_dict()


def test_safe_create_dict_drops_every_privilege_field():
    """`safe=True` 走的就是这个方法：三个布尔位显式排除（另加 `id`、`oauth_accounts`）。

    这是「注册不能自封超管」的实际执行点。用例直接断言产出字典，而不是断言
    `fastapi_users` 的实现细节——升级框架后语义若变了，这里会亮红灯。
    """
    create = UserCreate.model_validate(
        {
            "email": "a@example.com",
            "password": "password123",
            "is_superuser": True,
            "is_active": False,
            "is_verified": True,
        }
    )

    assert set(create.create_update_dict()) == {"email", "password"}


def test_superuser_create_dict_is_what_the_safe_path_protects_against():
    """反面对照：非 safe 路径会把三个布尔位带出去。

    有它在，「`safe=True` 是唯一的防线」这句话才有依据——若哪天
    `create_update_dict()` 与它等价，上面那条用例就会退化成空断言。
    """
    create = UserCreate.model_validate({"email": "a@example.com", "password": "password123", "is_superuser": True})

    assert create.create_update_dict_superuser()["is_superuser"] is True


def test_user_read_defaults_to_the_plain_user_role():
    """`UserRead.role` 默认 `user`：直接构造响应模型的地方（测试、内部拼接）不会
    因为漏传而拿到一个能进后台的角色。"""
    read = UserRead(id=uuid.uuid4(), email="a@example.com")

    assert read.role == ROLE_USER == "user"
    # 默认值也要落进响应体，否则 `/api/auth/me` 的前端判据会拿到 undefined
    assert read.model_dump()["role"] == ROLE_USER
