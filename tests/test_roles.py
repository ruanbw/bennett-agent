"""角色常量与解析（`app/roles.py`）。

三档 RBAC 的 HTTP 链路用例（注册、升降级、踢会话、最后超管守卫）已随全量/端到端层
移除；这里只保留纯逻辑：`Role` 的排序、宽松解析与合法取值集合。
"""

from app.roles import ALL_ROLES, ROLE_ADMIN, ROLE_SUPERADMIN, ROLE_USER, Role


def test_role_levels_are_ordered():
    assert Role.USER < Role.ADMIN < Role.SUPERADMIN
    assert Role.parse(ROLE_USER) is Role.USER
    assert Role.parse(ROLE_ADMIN) is Role.ADMIN
    assert Role.parse(ROLE_SUPERADMIN) is Role.SUPERADMIN


def test_role_parse_is_case_insensitive_and_lenient():
    assert Role.parse("ADMIN") is Role.ADMIN
    assert Role.parse("  superadmin ") is Role.SUPERADMIN
    # 非法值降级成 user：一个拼错的值不该让整个鉴权依赖抛 500
    assert Role.parse("admn") is Role.USER
    assert Role.parse(None) is Role.USER
    assert Role.parse("") is Role.USER


def test_all_roles_matches_the_string_constants():
    assert set(ALL_ROLES) == {ROLE_USER, ROLE_ADMIN, ROLE_SUPERADMIN}
