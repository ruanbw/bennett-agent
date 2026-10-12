"""FastAPI 服务装配（纯 API）。

前后端分离：这里只提供 `/api/*`，不再挂载前端构建产物。前端由 Vite 独立运行
（`cd frontend && npm run dev`），通过 `frontend/.env` 的 `VITE_API_BASE_URL`
直连本服务，因此跑起 Web 界面不再需要 `npm run build`。
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.auth import router as auth_router
from app.api.chat import router as api_router
from app.config import settings
from app.db import cleanup_expired_access_tokens, create_db_and_tables, engine
from app.services.http_client import close_http_client

logger = logging.getLogger(__name__)


async def _cleanup_expired_sessions_on_startup() -> None:
    """启动时清理过期会话行（best-effort）。

    过期行只被读过滤、从不被删（见 `app.db.cleanup_expired_access_tokens`），启动是
    无需额外定时任务就能兜住表增长的时机。清理失败只记警告：数据库被锁之类的瞬时
    故障不该把服务拦在启动之外。
    """
    try:
        removed = await cleanup_expired_access_tokens(settings.session_lifetime_seconds)
    except Exception:  # noqa: BLE001 - 维护动作的任何故障都必须与启动隔离
        logger.warning("启动清理过期会话失败（不影响启动）", exc_info=True)
        return

    if removed:
        logger.info("启动清理过期会话: 删除=%d", removed)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # 建表必须在处理任何请求之前完成：认证依赖会查 user / access_token 两张表。
    # 使用 create_all（而非 Alembic）的取舍见 app/db.py 的 create_db_and_tables。
    await create_db_and_tables()
    await _cleanup_expired_sessions_on_startup()

    try:
        yield
    finally:
        # try/finally：yield 点收到异常（例如上层取消）时也要走到关闭，
        # 否则共享 httpx 客户端泄漏、SQLite 的 WAL 文件不会合并回主库。
        # O-2：共享 httpx 客户端需要显式关闭，否则进程退出时连接会被硬切断
        try:
            await close_http_client()
        except Exception:  # noqa: BLE001 - 单个资源关闭失败不能遮蔽另一个资源
            logger.warning("关闭共享 HTTP 客户端失败", exc_info=True)
        # 释放 SQLite 连接，让 WAL 文件能被干净地合并回主库
        try:
            await engine.dispose()
        except Exception:  # noqa: BLE001 - 同上，两个清理动作互不遮蔽
            logger.warning("释放数据库连接失败", exc_info=True)


def _api_docs_enabled() -> bool:
    """是否暴露 /docs、/redoc 与 /openapi.json。

    默认 None 表示按环境自动决定：production 下这些页面会把全部端点结构与
    请求/响应模型白送给未认证访客，是纯侦察面，关掉；其余环境保留，本地调试离不开。
    """
    if settings.enable_api_docs is None:
        return settings.app_env != "production"
    return settings.enable_api_docs


def create_app() -> FastAPI:
    docs_enabled = _api_docs_enabled()
    app = FastAPI(
        title=f"{settings.assistant_name} Agent API",
        description="基于 FastAPI + PydanticAI + LiteLLM 的对话助手服务",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    # 前端与后端不同源（Vite 跑在自己的端口上），跨域是常态而非例外
    cors_origins = settings.get_cors_origins()
    if settings.cors_allow_credentials and "*" in cors_origins:
        # 浏览器对「通配来源 + 携带凭据」的响应一律拒绝，静默降级成跨域不可用
        # 比启动即报错难查得多；这里要求把来源写成具体域名。
        raise ValueError(
            'CORS 配置无效：cors_allow_credentials=True 时 cors_allowed_origins 不能包含 "*"，'
            "浏览器会拒绝该组合；请改成具体来源（如 https://app.example.com）。"
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth_router)
    app.include_router(api_router)

    return app


app = create_app()
