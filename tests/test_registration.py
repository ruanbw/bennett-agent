"""公开注册的行为锁：统一 202（防枚举）+ 可关闭（I-04/I-05）。"""

import pytest
from fastapi import HTTPException
from fastapi_users.exceptions import InvalidPasswordException, UserAlreadyExists

from app.api.auth import enforce_registration_enabled, register_account
from app.config import settings
from app.schemas.user import UserCreate


class _FakeUserManager:
    def __init__(self, outcome: str = "created") -> None:
        self.outcome = outcome
        self.created: list[UserCreate] = []

    async def create(self, payload: UserCreate, *, safe: bool, request=None):  # noqa: ANN001
        assert safe is True  # 注册绝不能走 superuser 字段路径
        if self.outcome == "exists":
            raise UserAlreadyExists()
        if self.outcome == "invalid":
            raise InvalidPasswordException(reason="密码至少需要 8 个字符")
        self.created.append(payload)
        return object()


def _payload() -> UserCreate:
    return UserCreate(email="new@example.com", password="LongEnoughPass123")


async def test_created_and_existing_email_return_the_same_202():
    created = _FakeUserManager("created")
    existing = _FakeUserManager("exists")

    first = await register_account(_payload(), None, created)
    second = await register_account(_payload(), None, existing)

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.body == second.body  # 不泄露邮箱是否存在
    assert len(created.created) == 1


async def test_invalid_password_still_reports_reason():
    manager = _FakeUserManager("invalid")

    with pytest.raises(HTTPException) as excinfo:
        await register_account(_payload(), None, manager)

    assert excinfo.value.status_code == 400
    assert "REGISTER_INVALID_PASSWORD" in excinfo.value.detail


async def test_registration_can_be_disabled(monkeypatch):
    monkeypatch.setattr(settings, "registration_enabled", False)

    with pytest.raises(HTTPException) as excinfo:
        await enforce_registration_enabled()

    assert excinfo.value.status_code == 403


def test_openapi_register_route_is_202_and_public():
    from app import server

    app = server.create_app()
    spec = app.openapi()
    register = spec["paths"]["/api/auth/register"]["post"]

    assert "202" in register["responses"]
    assert "已存在" in register["description"]
    assert "security" not in register  # 公开端点不能要求 cookie
