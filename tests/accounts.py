"""测试用的账号工厂。

角色是数据库里的数据（`user.role`），所以造管理员必须真的把角色写进库——这也是线上
唯一的路径（`seed_admin.py` 与管理后台的角色接口最终都落到同一个 `apply_role`）。

这些函数统一用**独立会话**写库，而不是走 HTTP：管理端角色接口要求操作者本身是
superadmin，用它来造第一个 superadmin 会形成先有鸡还是先有蛋的问题。
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import User, async_session_maker
from app.roles import Role
from app.services.auth import apply_role

PASSWORD = "hunter2hunter2"


def unique_email(prefix: str = "user") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}@example.com"


def register(client: TestClient, email: str, password: str = PASSWORD) -> str:
    """公开注册（完全开放），返回用户 id。"""
    response = client.post("/api/auth/register", json={"email": email, "password": password})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def login(client: TestClient, email: str, password: str = PASSWORD) -> None:
    response = client.post("/api/auth/login", data={"username": email, "password": password})
    assert response.status_code == 204, response.text


async def set_role_in_db(email: str, role: Role) -> None:
    """直接把角色写进库（绕过 HTTP，用于准备前置状态）。"""
    async with async_session_maker() as session:
        user = await session.scalar(select(User).where(User.email == email))
        assert user is not None, f"用户不存在: {email}"
        apply_role(user, role)
        await session.commit()


async def get_role_from_db(email: str) -> str:
    async with async_session_maker() as session:
        user = await session.scalar(select(User).where(User.email == email))
        assert user is not None, f"用户不存在: {email}"
        return user.role
