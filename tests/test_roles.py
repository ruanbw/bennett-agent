"""角色模型（三档 RBAC）：从注册到进入管理后台的完整链路。

覆盖的都是「接错线会静默失效」的契约：

- 公开注册出来的账号一律 `role=user`，客户端不能指定角色；
- `admin` 能进管理后台，但**不能**改别人角色、不能封管理员；
- `superadmin` 能改别人角色，但不能改自己、不能降级最后一个超管；
- 角色变更会立即踢掉对方的全部会话（权限变更不该等到对方重新登录才生效）。
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.roles import ALL_ROLES, ROLE_ADMIN, ROLE_SUPERADMIN, ROLE_USER, Role
from app.server import create_app
from tests.accounts import PASSWORD, get_role_from_db, login, register, set_role_in_db, unique_email


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


def make_superadmin(client: TestClient, prefix: str = "root") -> tuple[str, str]:
    """建一个已登录的 superadmin，返回 (email, id)。"""
    email = unique_email(prefix)
    user_id = register(client, email)
    asyncio.run(set_role_in_db(email, Role.SUPERADMIN))
    login(client, email)
    return email, user_id


def make_admin(client: TestClient, prefix: str = "ops") -> tuple[str, str]:
    email = unique_email(prefix)
    user_id = register(client, email)
    asyncio.run(set_role_in_db(email, Role.ADMIN))
    login(client, email)
    return email, user_id


# ---------------------------------------------------------------------
# 注册：开放，但不授予特权
# ---------------------------------------------------------------------
def test_registration_is_open_and_grants_no_privilege(client):
    email = unique_email()

    response = client.post("/api/auth/register", json={"email": email, "password": PASSWORD})

    assert response.status_code == 201, response.text
    assert response.json()["role"] == ROLE_USER
    assert response.json()["is_superuser"] is False
    assert response.json()["is_verified"] is False

    login(client, email)
    assert client.get("/api/auth/me").json()["role"] == ROLE_USER


def test_register_ignores_client_supplied_role_and_flags(client):
    """注册接口必须忽略客户端传来的 `role` / `is_superuser`。

    `BaseUserCreate` 里确实带 `is_superuser` / `is_active` / `is_verified`，但注册路由
    调用 `user_manager.create(..., safe=True)`，而 `safe=True` 会走 `create_update_dict()`，
    该方法显式排除这些字段。
    """
    response = client.post(
        "/api/auth/register",
        json={
            "email": unique_email(),
            "password": PASSWORD,
            "role": ROLE_SUPERADMIN,
            "is_superuser": True,
            "is_active": True,
            "is_verified": True,
        },
    )

    assert response.status_code == 201
    assert response.json()["role"] == ROLE_USER
    assert response.json()["is_superuser"] is False


def test_duplicate_registration_is_still_rejected(client):
    email = unique_email()
    assert client.post("/api/auth/register", json={"email": email, "password": PASSWORD}).status_code == 201

    duplicate = client.post("/api/auth/register", json={"email": email, "password": PASSWORD})

    assert duplicate.status_code == 400
    assert duplicate.json()["detail"] == "REGISTER_USER_ALREADY_EXISTS"


# ---------------------------------------------------------------------
# 角色变更：只有 superadmin 能做
#
# 权限门本身（匿名 401、user 403、admin 放行）在 `tests/test_admin.py` 的「权限」
# 一节，这里不再重复；superadmin 能进后台由下面每条用例隐式覆盖——它们全都要先
# 到达 `/api/admin/users/{id}/role` 才会拿到 200/403/400。
# ---------------------------------------------------------------------
def test_admin_cannot_change_roles(client):
    """admin 能进后台，但改角色是最高一档的专属动作。"""
    make_admin(client)
    target_id = register(client, unique_email("target"), PASSWORD)

    response = client.patch(f"/api/admin/users/{target_id}/role", json={"role": ROLE_ADMIN})

    assert response.status_code == 403
    assert "超级管理员" in response.json()["detail"]


def test_superadmin_promotes_a_user_to_admin_and_it_takes_effect(client):
    make_superadmin(client)
    email = unique_email("promote")
    user_id = register(client, email)

    response = client.patch(f"/api/admin/users/{user_id}/role", json={"role": ROLE_ADMIN})

    assert response.status_code == 200, response.text
    assert response.json()["role"] == ROLE_ADMIN
    assert response.json()["is_superuser"] is True

    # 角色落库了（不是只回显）
    assert asyncio.run(get_role_from_db(email)) == ROLE_ADMIN

    # 被改角色的用户**重新登录**后能进后台
    login(client, email)
    assert client.get("/api/admin/providers").status_code == 200


def test_superadmin_demotes_an_admin_back_to_user(client):
    make_superadmin(client)
    email = unique_email("demote")
    user_id = register(client, email)
    client.patch(f"/api/admin/users/{user_id}/role", json={"role": ROLE_ADMIN})

    response = client.patch(f"/api/admin/users/{user_id}/role", json={"role": ROLE_USER})

    assert response.status_code == 200
    assert response.json()["role"] == ROLE_USER
    assert response.json()["is_superuser"] is False

    login(client, email)
    assert client.get("/api/admin/providers").status_code == 403


def test_role_change_kills_the_target_session(client):
    """权限变更必须立即生效，不能等对方重新登录。"""
    make_superadmin(client)
    email = unique_email("session")
    user_id = register(client, email)

    with TestClient(create_app()) as victim:
        login(victim, email)
        assert victim.get("/api/auth/me").status_code == 200

        assert client.patch(f"/api/admin/users/{user_id}/role", json={"role": ROLE_ADMIN}).status_code == 200

        # 旧 token 已被删：下一次请求 401，而不是「还是普通用户」的 200
        assert victim.get("/api/auth/me").status_code == 401

        # 重新登录后拿到新角色
        login(victim, email)
        assert victim.get("/api/auth/me").json()["role"] == ROLE_ADMIN


def test_cannot_change_own_role(client):
    _, own_id = make_superadmin(client)

    response = client.patch(f"/api/admin/users/{own_id}/role", json={"role": ROLE_ADMIN})

    assert response.status_code == 400
    assert "自己" in response.json()["detail"]


def test_superadmin_role_cannot_be_granted_over_http(client):
    """最高一档只能经运维通道授予：允许 HTTP 授予的话，任何被接管的 admin 账号都能
    把同伙提到最高权限，而日志上看不出异常。
    """
    make_superadmin(client)
    user_id = register(client, unique_email("climb"))

    response = client.patch(f"/api/admin/users/{user_id}/role", json={"role": ROLE_SUPERADMIN})

    # pydantic 的 Literal 直接挡在入口：422，而不是走进业务分支
    assert response.status_code == 422


def test_role_payload_rejects_unknown_values(client):
    make_superadmin(client)
    user_id = register(client, unique_email("bad"))

    for bogus in ("admn", "", "root", "Admin "):
        response = client.patch(f"/api/admin/users/{user_id}/role", json={"role": bogus})
        assert response.status_code == 422, f"{bogus!r} 不该被接受"


def test_cannot_change_role_of_a_banned_account(client):
    make_superadmin(client)
    user_id = register(client, unique_email("banned"))
    assert client.post(f"/api/admin/users/{user_id}/ban").status_code == 200

    response = client.patch(f"/api/admin/users/{user_id}/role", json={"role": ROLE_ADMIN})

    assert response.status_code == 400
    assert "解封" in response.json()["detail"]


def test_setting_the_same_role_is_idempotent(client):
    make_superadmin(client)
    user_id = register(client, unique_email("same"))

    first = client.patch(f"/api/admin/users/{user_id}/role", json={"role": ROLE_ADMIN})
    second = client.patch(f"/api/admin/users/{user_id}/role", json={"role": ROLE_ADMIN})

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["role"] == ROLE_ADMIN


def test_unknown_user_is_404(client):
    make_superadmin(client)

    response = client.patch("/api/admin/users/00000000-0000-0000-0000-000000000000/role", json={"role": ROLE_ADMIN})
    assert response.status_code == 404


# ---------------------------------------------------------------------
# 最后一个超管不能被降级
# ---------------------------------------------------------------------
def test_last_superadmin_guard_uses_the_global_count():
    """直接锁守卫的判据：它算的是**库里还剩几个**超管，不是「除了操作者还剩几个」。

    不用 HTTP 测：操作者自己必须是超管，而「不能改自己」这条已先一步拦住，所以顺序
    执行时该守卫在 HTTP 路径上不可达；它真正防的是并发（A 降 B 与 B 降 A 同时进行），
    那种场景用 TestClient 无法稳定复现。
    """
    from app.services.auth import count_superadmins

    baseline = asyncio.run(count_superadmins())
    email = unique_email("count-root")

    with TestClient(create_app()) as client:
        register(client, email)
        assert asyncio.run(count_superadmins()) == baseline

        asyncio.run(set_role_in_db(email, Role.SUPERADMIN))
        assert asyncio.run(count_superadmins()) == baseline + 1

        asyncio.run(set_role_in_db(email, Role.ADMIN))
        assert asyncio.run(count_superadmins()) == baseline


def test_superadmin_can_demote_another_superadmin_when_more_remain(client):
    """两个超管时，降其中一个是允许的（守卫只在「只剩他一个」时才拦）。"""
    first_email, _ = make_superadmin(client, "root-a")
    second_email = unique_email("root-b")
    second_id = register(client, second_email)
    asyncio.run(set_role_in_db(second_email, Role.SUPERADMIN))

    assert client.patch(f"/api/admin/users/{second_id}/role", json={"role": ROLE_ADMIN}).status_code == 200
    assert asyncio.run(get_role_from_db(second_email)) == ROLE_ADMIN
    assert asyncio.run(get_role_from_db(first_email)) == ROLE_SUPERADMIN


# ---------------------------------------------------------------------
# 封禁与角色的关系
#
# 封禁/解封的等级守卫（自己、同级、超管）在 `tests/test_admin.py` 的「用户封禁」
# 一节，那里对同一个 actor 把三种情况都测了。本文件只关心角色链路。
# ---------------------------------------------------------------------


# ---------------------------------------------------------------------
# 角色常量与解析
# ---------------------------------------------------------------------
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
