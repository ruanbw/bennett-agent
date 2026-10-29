"""FastAPI 依赖注入：请求级会话，以及 fastapi-users 的 user / access-token 后端。"""

from collections.abc import AsyncGenerator

from fastapi import Depends
from fastapi_users.db import SQLAlchemyUserDatabase
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import async_session_maker
from app.db.models.user import AccessToken, HashedAccessTokenDatabase, User


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session


async def get_user_db(session: AsyncSession = Depends(get_async_session)):  # noqa: B008
    yield SQLAlchemyUserDatabase(session, User)


async def get_access_token_db(session: AsyncSession = Depends(get_async_session)):  # noqa: B008
    yield HashedAccessTokenDatabase(session, AccessToken)
