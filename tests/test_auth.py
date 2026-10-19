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
4. 会话生命周期：过期行被服务端拒绝并被清理；登出 / 改密 / 停用账号即时失效；
   多账号互不干扰；`lifetime_seconds<=0` 直接报错而不是永不过期。
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from fastapi_users.db import SQLAlchemyUserDatabase
from fastapi_users.schemas import BaseUserUpdate
from sqlalchemy import select, update

from app.config import settings
from app.db import (
    AccessToken,
    User,
    async_session_maker,
    cleanup_expired_access_tokens,
    create_db_and_tables,
    delete_user_access_tokens,
)
from app.server import create_app
from app.services.auth import MIN_PASSWORD_LENGTH, UserManager, get_database_strategy

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


def register_body(email, password=PASSWORD):
    return {"email": email, "password": password}


def register(client, email, password=PASSWORD):
    return client.post("/api/auth/register", json=register_body(email, password))


def login(client, email, password=PASSWORD):
    """登录走**表单编码**。

    fastapi-users 的登录端点复用 `OAuth2PasswordRequestForm`，所以字段名是 `username`
    （填邮箱）而不是 `email`，且必须是 form 而不是 json。
    """
    return client.post("/api/auth/login", data={"username": email, "password": password})


def replay_session_cookie(client, token):
    """把指定 token 装回 cookie jar，模拟「客户端手里还留着旧 cookie」。

    domain 必须显式给：登录响应下发的是 host-only cookie，jar 里的 domain 是
    `testserver.local`（http.cookiejar 会把不含点的 host 补成 `.local`）；而
    `client.cookies.set(name, value)` 的默认 domain 是空串，**那种条目不会被任何请求
    发出**——回放就成了空操作，会让「旧 token 依然 401」这类断言假通过。
    """
    client.cookies.set("bennett_session", token, domain="testserver.local")


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
    """登出会真的删掉服务端会话记录，所以把登出前的 cookie 原样塞回去也恢复不了。"""
    email = unique_email()
    register(client, email)
    login(client, email)
    assert client.get("/api/auth/me").status_code == 200

    before_logout = client.cookies.get("bennett_session")
    assert before_logout

    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401

    replay_session_cookie(client, before_logout)
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
    """注册接口必须忽略客户端传来的 `is_superuser` / `is_active` / `is_verified`。

    `BaseUserCreate` 里带着这三个字段，但注册路由调用 `user_manager.create(..., safe=True)`，
    而 `safe=True` 会走 `create_update_dict()`，该方法显式排除它们。
    """
    response = client.post(
        "/api/auth/register",
        json={
            **register_body(unique_email()),
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

    前端靠这个区分「密码错了」和「会话过期了」。
    """
    email = unique_email()
    register(client, email)

    response = login(client, email, password="wrong-password")

    assert response.status_code == 400
    assert response.json()["detail"] == "LOGIN_BAD_CREDENTIALS"


def test_unknown_user_login_is_rejected(client):
    assert login(client, unique_email()).status_code == 400


def test_login_requires_form_encoding(client):
    """发 JSON 会得到 422，而不是「密码错误」——登录契约是表单编码。"""
    email = unique_email()
    register(client, email)

    response = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})

    assert response.status_code == 422


def test_duplicate_registration_is_rejected(client):
    email = unique_email()

    assert register(client, email).status_code == 201
    assert register(client, email).status_code == 400


def test_register_rejects_short_password(client):
    """密码下限的回归锁：基类的 `validate_password` 是**空实现**，不覆盖就能用 1 个
    字符注册成功（已实测 201）。
    """
    response = client.post(
        "/api/auth/register",
        json=register_body(unique_email(), "x" * (MIN_PASSWORD_LENGTH - 1)),
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "REGISTER_INVALID_PASSWORD"
    assert str(MIN_PASSWORD_LENGTH) in detail["reason"]


def test_register_accepts_password_at_the_limit(client):
    response = client.post(
        "/api/auth/register",
        json=register_body(unique_email(), "x" * MIN_PASSWORD_LENGTH),
    )

    assert response.status_code == 201


# ---------------------------------------------------------------------
# 会话生命周期（服务端会话的过期与失效语义）
# ---------------------------------------------------------------------
async def _age_session(token: str, seconds: int) -> None:
    """把会话行的 `created_at` 往前拨，等价于「这个会话是 N 秒前登录的」。

    比 sleep 更确定，也不让用例变慢。
    """
    async with async_session_maker() as session:
        await session.execute(
            update(AccessToken)
            .where(AccessToken.token == token)
            .values(created_at=datetime.now(timezone.utc) - timedelta(seconds=seconds))
        )
        await session.commit()


async def _create_user() -> User:
    """直接建一个用户行，用于会话维护函数的纯 DB 用例（不走 HTTP）。"""
    async with async_session_maker() as session:
        user = User(email=unique_email(), hashed_password="not-a-real-hash", is_active=True)
        session.add(user)
        await session.commit()
        return user


async def _create_session(user_id, age_seconds: int = 0) -> str:
    """直接建一条会话行（`token` 列宽 43，uuid4 hex 是 32 位，够用）。"""
    token = uuid.uuid4().hex
    async with async_session_maker() as session:
        session.add(
            AccessToken(
                token=token,
                user_id=user_id,
                created_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
            )
        )
        await session.commit()
    return token


async def _tokens_still_present(*tokens: str) -> set[str]:
    async with async_session_maker() as session:
        result = await session.execute(select(AccessToken.token).where(AccessToken.token.in_(tokens)))
        return set(result.scalars())


async def test_expired_session_is_rejected(client, monkeypatch):
    """过期判定在服务端：时间一到，同一个 cookie 立刻 401。

    lifetime 用 monkeypatch 调小，顺带锁住「get_database_strategy 真的把 settings 的值
    传进了 DatabaseStrategy」——否则把行拨老也不会被拒绝。改完由 monkeypatch 自动恢复。
    """
    monkeypatch.setattr(settings, "session_lifetime_seconds", 60)
    email = unique_email()
    assert register(client, email).status_code == 201
    assert login(client, email).status_code == 204
    assert client.get("/api/auth/me").status_code == 200

    token = client.cookies.get("bennett_session")
    assert token
    await _age_session(token, seconds=120)

    assert client.get("/api/auth/me").status_code == 401


async def test_cleanup_expired_access_tokens_deletes_only_expired_rows():
    """清理函数删的是 `created_at < now - lifetime` 的行，未过期的行一条不动。"""
    await create_db_and_tables()
    user = await _create_user()
    expired = await _create_session(user.id, age_seconds=2 * 3600)
    fresh = await _create_session(user.id, age_seconds=0)

    removed = await cleanup_expired_access_tokens(lifetime_seconds=3600)

    remaining = await _tokens_still_present(expired, fresh)
    assert expired not in remaining
    assert fresh in remaining
    # 库里还有别的用例的行，这里只断言「确实删了」；精确计数由 delete_user_access_tokens 用例锁住
    assert removed >= 1


async def test_delete_user_access_tokens_removes_only_that_user():
    """踢人 / 改密用的删行只按 user_id 过滤，别的账号不受影响。"""
    await create_db_and_tables()
    user_a = await _create_user()
    user_b = await _create_user()
    a1 = await _create_session(user_a.id)
    a2 = await _create_session(user_a.id)
    b1 = await _create_session(user_b.id)

    removed = await delete_user_access_tokens(user_a.id)

    assert removed == 2
    assert await _tokens_still_present(a1, a2, b1) == {b1}


def test_sessions_of_different_accounts_are_independent(client):
    """两个账号各自的会话互不干扰：A 登出不会踢 B 下线。"""
    email_a, email_b = unique_email(), unique_email()
    assert register(client, email_a).status_code == 201
    assert register(client, email_b).status_code == 201
    assert login(client, email_a).status_code == 204
    token_a = client.cookies.get("bennett_session")
    assert login(client, email_b).status_code == 204
    token_b = client.cookies.get("bennett_session")
    assert token_a and token_b and token_a != token_b

    replay_session_cookie(client, token_a)
    response_a = client.get("/api/auth/me")
    replay_session_cookie(client, token_b)
    response_b = client.get("/api/auth/me")
    assert response_a.status_code == 200 and response_a.json()["email"] == email_a
    assert response_b.status_code == 200 and response_b.json()["email"] == email_b

    replay_session_cookie(client, token_a)
    assert client.post("/api/auth/logout").status_code == 204
    replay_session_cookie(client, token_b)
    assert client.get("/api/auth/me").status_code == 200


async def test_inactive_user_session_is_rejected(client):
    """停用账号（is_active=False）后旧 cookie 立刻 401，不需要等服务端删行。"""
    email = unique_email()
    assert register(client, email).status_code == 201
    assert login(client, email).status_code == 204
    assert client.get("/api/auth/me").status_code == 200

    async with async_session_maker() as session:
        await session.execute(update(User).where(User.email == email).values(is_active=False))
        await session.commit()

    assert client.get("/api/auth/me").status_code == 401


async def test_update_revokes_every_session_of_that_user(client):
    """改密（user_manager.update）后，该用户所有旧 token 全部 401。

    直接驱动 UserManager 而不是走 HTTP：当前没有挂改密 / 用户更新路由，但
    `on_after_update` 就是将来那些路由会自动走到的钩子，这里把它锁住。
    """
    email = unique_email()
    assert register(client, email).status_code == 201
    assert login(client, email).status_code == 204
    first = client.cookies.get("bennett_session")
    assert login(client, email).status_code == 204
    second = client.cookies.get("bennett_session")
    assert first and second and first != second

    async with async_session_maker() as session:
        user_db = SQLAlchemyUserDatabase(session, User)
        user = await user_db.get_by_email(email)
        assert user is not None
        await UserManager(user_db).update(BaseUserUpdate(password="new-password-123"), user)

    assert await _tokens_still_present(first, second) == set()

    for token in (first, second):
        replay_session_cookie(client, token)
        assert client.get("/api/auth/me").status_code == 401

    # 旧会话全废，但新密码仍可重新登录（删行不影响凭据）
    client.cookies.clear()
    assert login(client, email, password="new-password-123").status_code == 204
    assert client.get("/api/auth/me").status_code == 200


def test_expired_cleanup_failure_does_not_break_login(client, monkeypatch, caplog):
    """登录里的清理是 best-effort：清理抛错也必须照常登录，只记一条 warning。"""
    email = unique_email()
    assert register(client, email).status_code == 201

    async def boom(_lifetime_seconds):
        raise RuntimeError("database is locked")

    monkeypatch.setattr("app.services.auth.cleanup_expired_access_tokens", boom)

    with caplog.at_level(logging.WARNING):
        assert login(client, email).status_code == 204
    assert any("清理过期会话失败" in record.getMessage() for record in caplog.records)
    assert client.get("/api/auth/me").status_code == 200


@pytest.mark.parametrize("lifetime", [0, -1, -3600])
def test_get_database_strategy_rejects_non_positive_lifetime(monkeypatch, lifetime):
    """`lifetime_seconds<=0` 必须拒绝：框架用真值判断过期，0 会让会话**永不过期**。"""
    monkeypatch.setattr(settings, "session_lifetime_seconds", lifetime)

    with pytest.raises(ValueError, match="永不过期"):
        # 校验发生在使用适配器之前，所以这里不需要真数据库
        get_database_strategy(None)


@pytest.mark.parametrize("lifetime", [0, -1])
async def test_cleanup_rejects_non_positive_lifetime(lifetime):
    """清理函数同样拒绝非正值，否则 cutoff=now 会把全部有效会话删光。"""
    with pytest.raises(ValueError, match="永不过期"):
        await cleanup_expired_access_tokens(lifetime)
