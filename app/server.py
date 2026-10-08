"""FastAPI 服务装配与静态资源挂载。"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.chat import router as api_router
from app.config import settings
from app.services.http_client import close_http_client

logger = logging.getLogger(__name__)

#: 前端构建产物目录。与 config/prompts 一致地使用 pathlib，不再混用 os.path。
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    if not FRONTEND_DIST.is_dir():
        # R-8：此前挂载是静默条件分支——没有 dist 时服务照常启动、打印启动横幅，
        # 然后 GET / 返回 404，控制台里没有任何线索指向原因。
        logger.warning(
            "未找到前端构建产物 %s，Web 界面将不可用（API 仍可用）。"
            "请先执行：cd frontend && npm install && npm run build",
            FRONTEND_DIST,
        )

    yield

    # O-2：共享 httpx 客户端需要显式关闭，否则进程退出时连接会被硬切断
    await close_http_client()


def create_app() -> FastAPI:
    app = FastAPI(
        title=f"{settings.assistant_name} Agent API",
        description="基于 FastAPI + PydanticAI + LiteLLM 的对话助手服务",
        version="1.0.0",
        lifespan=lifespan,
    )

    # 允许跨域请求（不开 allow_credentials，避免「通配来源 + 携带凭证」的组合）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.get_cors_origins(),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router)

    # 前端已构建时直接挂载静态文件。必须放在 API 路由之后注册，
    # 否则挂在 "/" 的 StaticFiles 会优先吃掉 /api/*。
    if FRONTEND_DIST.is_dir():
        app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")

    return app


app = create_app()
