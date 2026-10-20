"""FastAPI 服务装配（纯 API）。

前后端分离：这里只提供 `/api/*`，不挂载前端构建产物。前端由 Vite 独立运行
（`cd frontend && npm run dev`），通过 `frontend/.env` 的 `VITE_API_BASE_URL`
直连本服务。
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.chat import router as api_router
from app.api.sync import router as sync_router
from app.config import settings
from app.db import cleanup_expired_access_tokens, create_db_and_tables, engine
from app.services.http_client import close_http_client
from app.services.model_service import seed_default_provider

logger = logging.getLogger(__name__)


async def _cleanup_expired_sessions_on_startup() -> None:
    """启动时清理过期会话行（best-effort）。

    启动是无需额外定时任务就能兜住过期行增长的时机（为何需要显式清理见
    `app.db.cleanup_expired_access_tokens`）。清理失败只记警告：数据库被锁之类的
    瞬时故障不该把服务拦在启动之外。
    """
    try:
        removed = await cleanup_expired_access_tokens(settings.session_lifetime_seconds)
    except Exception:  # noqa: BLE001 - 维护动作的任何故障都必须与启动隔离
        logger.warning("启动清理过期会话失败（不影响启动）", exc_info=True)
        return

    if removed:
        logger.info("启动清理过期会话: 删除=%d", removed)


def _warn_if_auth_rate_limit_is_global() -> None:
    """反代部署 + 未配 `trusted_proxy_headers` 时告警：认证限流会退化成全站单桶。

    为什么只告警不报错：单进程直连部署（本机开发、反代直连后端）下
    `request.client.host` 就是真实客户端地址，限流工作正常。只有「中间还隔着一个反代」
    时才会把所有请求看成一个来源。这里无法从代码侧判断部署形态，只能在启动日志里
    明确说出这个组合与后果——否则运维只会看到「登录莫名 429，等一分钟就好」。

    同一个坑在对话端点已经被绕开了（改成按账号计数，见 `app/api/chat.py`），
    认证端点不行：登录/注册本来就没有账号身份可用。
    """
    if settings.auth_rate_limit_per_minute <= 0:
        return
    if settings.trusted_proxy_headers.strip():
        return
    logger.warning(
        "认证限流按 client.host 计数，而 TRUSTED_PROXY_HEADERS 为空：若本服务部署在反向代理之后，"
        "所有请求会被算作同一个来源（全站共享 %d 次/分钟）。请在代理上设置 X-Forwarded-For 并配置"
        " TRUSTED_PROXY_HEADERS=X-Forwarded-For，或直连后端。",
        settings.auth_rate_limit_per_minute,
    )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # 建表必须在处理任何请求之前完成：认证依赖会查 user / access_token 两张表。
    # 使用 create_all + 手写幂等补列（而非 Alembic）的取舍见 app/db.py。
    await create_db_and_tables()

    # 首次启动（或库里一条提供商都没有）时，用环境变量引导出一条「默认网关」，
    # 让管理后台还没配置时也不至于没有任何模型可用。只写库不联网。
    try:
        if await seed_default_provider():
            logger.info("模型提供商已从环境变量完成引导，可在管理后台继续维护")
    except Exception:  # noqa: BLE001 - 引导失败不应阻止服务启动
        logger.warning("模型提供商引导失败（不影响启动）", exc_info=True)

    await _cleanup_expired_sessions_on_startup()
    _warn_if_auth_rate_limit_is_global()

    try:
        yield
    finally:
        # try/finally：yield 点收到异常（例如上层取消）时也要走到关闭，
        # 否则共享 httpx 客户端泄漏、SQLite 的 WAL 文件不会合并回主库。
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
        # 跨域前端要能读到限流的配额信息。不写这一行的话浏览器只放行 6 个简单响应头，
        # `fetch` 拿到的是 `null`——前端没法告诉用户「还要等几秒」。
        expose_headers=["Retry-After", "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset"],
    )

    app.include_router(auth_router)
    app.include_router(api_router)
    app.include_router(admin_router)
    app.include_router(sync_router)

    return app


app = create_app()
