"""认证依赖的会话生命周期（H-01 回归锁）。

H-01 的根因是「请求级 session 被 SSE/长轮询持有」。修复后认证只在一个短会话里完成，
流式端点不再依赖 `get_async_session`。这里锁住两件事：

1. `_read_user_from_token` 的语义与框架一致：有效 token → 用户；过期/停用/无 token → None；
2. `/api/chat` 与 `/api/sync/pull` 的依赖树里没有 `get_async_session`——一旦有人
   把认证改回请求级依赖，这条用例会立刻失败，而不是等到线上连接池被占满。
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from sqlalchemy import select
from starlette.requests import Request

from app import server
from app.config import settings
from app.db import (
    AccessToken,
    User,
    async_session_maker,
    create_db_and_tables,
    get_async_session,
)
from app.services.auth import _read_user_from_token, current_active_user


def _walk_dependencies(dependant):  # noqa: ANN001, ANN202
    yield dependant
    for child in dependant.dependencies:
        yield from _walk_dependencies(child)


def _iter_routes(routes):  # noqa: ANN001, ANN202
    """递归遍历路由：新版 FastAPI 用 `_IncludedRouter` 嵌套子路由，不再拍平。"""
    for route in routes:
        nested = getattr(route, "routes", None)
        if nested:
            yield from _iter_routes(nested)
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _iter_routes(original.routes)
        if isinstance(route, APIRoute):
            yield route


def _route(app, path: str, method: str) -> APIRoute:  # noqa: ANN001
    for route in _iter_routes(app.routes):
        if route.path == path and method in route.methods:
            return route
    raise AssertionError(f"未找到路由 {method} {path}")


def test_streaming_routes_do_not_hold_a_request_scoped_session():
    """流式/长轮询端点不能依赖请求级 DB session（否则整条流期间占着连接）。"""
    app = server.create_app()

    for path, method in (("/api/chat", "POST"), ("/api/sync/pull", "GET")):
        route = _route(app, path, method)
        calls = [dep.call for dep in _walk_dependencies(route.dependant)]
        assert get_async_session not in calls, f"{method} {path} 仍依赖请求级 session"


async def test_read_user_from_token_roundtrip_and_guards():
    await create_db_and_tables()
    user_id = uuid.uuid4()
    token = f"test-token-{uuid.uuid4().hex}"
    email = f"session-{uuid.uuid4().hex[:12]}@example.com"

    async with async_session_maker() as session:
        session.add(
            User(
                id=user_id,
                email=email,
                hashed_password="x",
                is_active=True,
                is_superuser=False,
                is_verified=True,
            )
        )
        await session.commit()
    # 先提交用户再插 token：两张表之间没有 ORM relationship，同一次 flush 不保证
    # 父行先写，SQLite 开着外键时会直接 FOREIGN KEY constraint failed。
    async with async_session_maker() as session:
        session.add(AccessToken(token=token, user_id=user_id, created_at=datetime.now(timezone.utc)))
        await session.commit()

    try:
        user = await _read_user_from_token(token)
        assert user is not None
        assert user.id == user_id
        # detach 之后这些属性必须仍然可读（流式期间下游只读它们）
        assert user.email == email
        assert user.is_active is True
        assert user.role_level.name == "USER"

        assert await _read_user_from_token(None) is None
        assert await _read_user_from_token("not-a-real-token") is None

        cookie_request = Request(
            {
                "type": "http",
                "headers": [(b"cookie", f"{settings.auth_cookie_name}={token}".encode("ascii"))],
            }
        )
        assert (await current_active_user(cookie_request)).id == user_id

        # 停用账号：helper 仍返回用户，由 current_active_user 拒绝（active=True 语义）
        async with async_session_maker() as session:
            row = await session.get(User, user_id)
            row.is_active = False
            await session.commit()
        inactive = await _read_user_from_token(token)
        assert inactive is not None and inactive.is_active is False
        with pytest.raises(HTTPException) as excinfo:
            await current_active_user(cookie_request)
        assert excinfo.value.status_code == 401

        # 过期会话：DatabaseStrategy 按 created_at 过滤，不能自己写 token == 的查询
        async with async_session_maker() as session:
            row = await session.get(User, user_id)
            row.is_active = True
            token_row = await session.scalar(select(AccessToken).where(AccessToken.token == token))
            token_row.created_at = datetime.now(timezone.utc) - timedelta(days=3650)
            await session.commit()
        assert await _read_user_from_token(token) is None
    finally:
        async with async_session_maker() as session:
            token_row = await session.scalar(select(AccessToken).where(AccessToken.token == token))
            if token_row is not None:
                await session.delete(token_row)
            user_row = await session.get(User, user_id)
            if user_row is not None:
                await session.delete(user_row)
            await session.commit()
