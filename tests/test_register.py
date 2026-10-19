"""邀请码注册。

旧路径是「`on_after_register` 看到白名单邮箱就提权」：项目没有邮箱验证，邮箱字符串
本身不能证明归属，所以任何人在白名单里写下 `ops@example.com` 之后，抢注这个地址的
人就立刻成为超管。现在提权必须同时满足两个条件——邀请码正确、邮箱在白名单——而邀请码
只有部署者掌握。

覆盖的是用户可观察的结果：注册被拒的状态码与文案、注册后的登录能力、以及管理接口
的权限（403 / 200），不看内部调用。
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.server import create_app

PASSWORD = "hunter2hunter2"
INVITE_CODE = "correct-horse-battery-staple"


def unique_email(prefix: str = "register") -> str:
    """每个用例用独立邮箱；整个测试会话共用一个临时库（见 conftest.py）。"""
    return f"{prefix}-{uuid.uuid4().hex[:12]}@example.com"


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def invite_configured(monkeypatch):
    """配好邀请码（默认 conftest 也给了一个，这里换成用例自己的字面值）。"""
    monkeypatch.setattr(settings, "registration_invite_code", INVITE_CODE)


def register(client: TestClient, email: str, invite_code: str, password: str = PASSWORD):
    return client.post(
        "/api/auth/register",
        json={"email": email, "password": password, "invite_code": invite_code},
    )


def login(client: TestClient, email: str, password: str = PASSWORD):
    return client.post("/api/auth/login", data={"username": email, "password": password})


# ---------------------------------------------------------------------
# 注册关闭
# ---------------------------------------------------------------------
def test_registration_is_closed_when_no_invite_code_is_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "registration_invite_code", "")

    response = register(client, unique_email(), invite_code="whatever")

    assert response.status_code == 403
    assert "注册已关闭" in response.json()["detail"]


# ---------------------------------------------------------------------
# 邀请码校验
# ---------------------------------------------------------------------
def test_wrong_invite_code_is_rejected_and_does_not_leak_the_expected_value(client, invite_configured):
    response = register(client, unique_email(), invite_code="not-the-code")

    assert response.status_code == 403
    assert response.json()["detail"] == "邀请码不正确"
    assert INVITE_CODE not in response.text


def test_missing_invite_code_is_rejected(client, invite_configured):
    email = unique_email()

    response = client.post("/api/auth/register", json={"email": email, "password": PASSWORD})

    assert response.status_code == 403
    assert login(client, email).status_code == 400


def test_wrong_invite_code_cannot_claim_a_whitelisted_email(client, invite_configured, monkeypatch):
    """S1 的回归锁：白名单邮箱 + 错误的邀请码，既建不了号也拿不到超管。"""
    email = unique_email("ops")
    monkeypatch.setattr(settings, "superuser_emails", email)

    response = register(client, email, invite_code="not-the-code")

    assert response.status_code == 403
    # 账号根本没建出来：拿正确密码也登不进去
    assert login(client, email).status_code == 400


def test_correct_invite_code_creates_an_account_that_can_log_in(client, invite_configured):
    email = unique_email()

    response = register(client, email, invite_code=INVITE_CODE)

    assert response.status_code == 201
    assert response.json()["email"] == email
    assert response.json()["is_superuser"] is False

    assert login(client, email).status_code == 204
    assert client.get("/api/auth/me").json()["email"] == email


def test_duplicate_registration_is_still_rejected_with_the_same_semantics(client, invite_configured):
    email = unique_email()
    assert register(client, email, invite_code=INVITE_CODE).status_code == 201

    duplicate = register(client, email, invite_code=INVITE_CODE)

    assert duplicate.status_code == 400
    assert duplicate.json()["detail"] == "REGISTER_USER_ALREADY_EXISTS"


# ---------------------------------------------------------------------
# 提权：邀请码正确 + 邮箱在白名单
# ---------------------------------------------------------------------
def test_whitelisted_email_becomes_superuser_with_a_valid_invite_code(client, invite_configured, monkeypatch):
    email = unique_email("ops")
    monkeypatch.setattr(settings, "superuser_emails", email)

    assert register(client, email, invite_code=INVITE_CODE).status_code == 201
    assert login(client, email).status_code == 204

    assert client.get("/api/auth/me").json()["is_superuser"] is True
    assert client.get("/api/admin/providers").status_code == 200


def test_non_whitelisted_email_stays_a_regular_user(client, invite_configured):
    email = unique_email()

    assert register(client, email, invite_code=INVITE_CODE).status_code == 201
    assert login(client, email).status_code == 204

    assert client.get("/api/auth/me").json()["is_superuser"] is False
    assert client.get("/api/admin/providers").status_code == 403
