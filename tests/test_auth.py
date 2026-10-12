"""认证网关（cookie 会话）与安全断言。

## 为什么这里用真注册/真登录，而限流用例用依赖覆盖

`test_rate_limit.py` 替换掉登录态依赖以隔离关注点；本文件正相反——它的**被测对象
就是认证本身**，所以必须走真实的注册 → 登录 → 带 cookie → 登出流程。只有真跑一遍，
才能覆盖「cookie 是否真的下发」「登出是否真的在服务端失效」这类接线问题，
依赖覆盖会把它们全部短路掉。

## 覆盖范围

1. 网关：未登录访问 `/api/*` 一律 401。
2. 完整流程：注册 → 登录 → 访问 → 登出 → 再访问变 401。
3. 安全断言：注册不能自封管理员；登录失败与未登录是**不同**状态码。
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.server import create_app
from app.services.auth import MIN_PASSWORD_LENGTH

PASSWORD = "hunter2hunter2"

#: 受登录保护的端点。认证接口自身（/api/auth/*）不在其中。
PROTECTED = [
    ("GET", "/api/models"),
    ("POST", "/api/chat/title"),
    ("POST", "/api/chat"),
]


def unique_email() -> str:
    """每个用例用独立邮箱。

    整个测试会话共用一个临时 SQLite 库（见 conftest.py），因此不能假设库是空的。
    """
    return f"user-{uuid.uuid4().hex[:12]}@example.com"


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


def register(client, email, password=PASSWORD):
    return client.post("/api/auth/register", json={"email": email, "password": password})


def login(client, email, password=PASSWORD):
    """登录走**表单编码**。

    fastapi-users 的登录端点复用 `OAuth2PasswordRequestForm`，所以字段名是 `username`
    （填邮箱）而不是 `email`，且必须是 form 而不是 json。
    """
    return client.post("/api/auth/login", data={"username": email, "password": password})


@pytest.fixture
def logged_in(client):
    email = unique_email()
    assert register(client, email).status_code == 201
    assert login(client, email).status_code == 204
    return client, email


# ---------------------------------------------------------------------
# 网关
# ---------------------------------------------------------------------
@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_protected_endpoints_reject_anonymous(client, method, path):
    body = {"messages": [{"role": "user", "content": "hi"}]} if path == "/api/chat" else {"prompt": "hi"}

    response = client.request(method, path, json=body)

    assert response.status_code == 401


def test_me_rejects_anonymous(client):
    assert client.get("/api/auth/me").status_code == 401


# ---------------------------------------------------------------------
# 完整流程
# ---------------------------------------------------------------------
def test_register_login_access(logged_in):
    client, email = logged_in

    me = client.get("/api/auth/me")

    assert me.status_code == 200
    assert me.json()["email"] == email
    assert client.get("/api/models").status_code == 200


def test_register_response_never_leaks_password(client):
    response = register(client, unique_email())

    payload = response.json()
    assert response.status_code == 201
    assert "hashed_password" not in payload
    assert "password" not in payload


def test_login_sets_httponly_lax_cookie(client):
    """cookie 属性直接决定安全性：JS 读不到（HttpOnly）、不跨站携带（SameSite）。"""
    email = unique_email()
    register(client, email)

    response = login(client, email)

    raw = response.headers["set-cookie"]
    lowered = raw.lower()
    assert raw.startswith("bennett_session=")
    assert "httponly" in lowered
    assert "samesite=lax" in lowered


def test_logout_revokes_session_server_side(client):
    """**这是选会话而非 JWT 的核心理由。**

    JWT 的登出只是让客户端删掉本地副本，令牌在过期前依然有效。这里登出会真的删掉
    服务端会话记录，所以把登出前的 cookie 原样塞回去也无法恢复登录态。
    """
    email = unique_email()
    register(client, email)
    login(client, email)
    assert client.get("/api/auth/me").status_code == 200

    before_logout = client.cookies.get("bennett_session")
    assert before_logout

    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401

    client.cookies.set("bennett_session", before_logout)
    assert client.get("/api/auth/me").status_code == 401


def test_each_login_gets_a_distinct_session(client):
    """多端登录：两次登录产生不同会话。"""
    email = unique_email()
    register(client, email)

    login(client, email)
    first = client.cookies.get("bennett_session")

    login(client, email)
    second = client.cookies.get("bennett_session")

    assert first and second
    assert first != second


# ---------------------------------------------------------------------
# 安全断言
# ---------------------------------------------------------------------
def test_register_cannot_self_promote_to_superuser(client):
    """注册接口必须忽略客户端传来的 `is_superuser`。

    `BaseUserCreate` 里确实带 `is_superuser` / `is_active` / `is_verified`，但注册路由
    调用 `user_manager.create(..., safe=True)`，而 `safe=True` 会走 `create_update_dict()`，
    该方法显式排除这三个字段。本用例是这条链路的回归锁——框架升级若改掉这个行为，
    这里必须失败。
    """
    response = client.post(
        "/api/auth/register",
        json={
            "email": unique_email(),
            "password": PASSWORD,
            "is_superuser": True,
            "is_active": True,
            "is_verified": True,
        },
    )

    assert response.status_code == 201
    assert response.json()["is_superuser"] is False
    assert response.json()["is_verified"] is False


def test_login_failure_is_400_not_401(client):
    """登录失败 400（凭据问题），未登录访问 401（缺登录态）。

    前端要靠这个区分「密码错了」和「会话过期了」，两者该给的提示完全不同。
    """
    email = unique_email()
    register(client, email)

    response = login(client, email, password="wrong-password")

    assert response.status_code == 400
    assert response.json()["detail"] == "LOGIN_BAD_CREDENTIALS"


def test_unknown_user_login_is_rejected(client):
    assert login(client, unique_email()).status_code == 400


def test_login_requires_form_encoding(client):
    """发 JSON 会得到 422，而不是「密码错误」。

    这条用例把「登录用表单编码」这个约定钉住——否则前端很容易写成 JSON，
    然后收到一个看不出原因的 422。
    """
    email = unique_email()
    register(client, email)

    response = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})

    assert response.status_code == 422


def test_duplicate_registration_is_rejected(client):
    email = unique_email()

    assert register(client, email).status_code == 201
    assert register(client, email).status_code == 400


def test_register_rejects_short_password(client):
    """密码下限的回归锁。

    基类的 `validate_password` 是**空实现**（源码里只有一句「你应该重载这个方法」），
    不覆盖就能用 1 个字符注册成功（已实测 201）。这里锁住覆盖行为：若框架升级后改了
    默认行为、或有人误删了这个覆盖，本用例必须失败。
    """
    response = client.post(
        "/api/auth/register",
        json={"email": unique_email(), "password": "x" * (MIN_PASSWORD_LENGTH - 1)},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "REGISTER_INVALID_PASSWORD"
    assert str(MIN_PASSWORD_LENGTH) in detail["reason"]


def test_register_accepts_password_at_the_limit(client):
    response = client.post(
        "/api/auth/register",
        json={"email": unique_email(), "password": "x" * MIN_PASSWORD_LENGTH},
    )

    assert response.status_code == 201
