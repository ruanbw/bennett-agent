"""FastAPI 服务装配（纯 API）。

前后端分离：这里只提供 `/api/*`，不再挂载前端构建产物。前端由 Vite 独立运行
（`cd frontend && npm run dev`），通过 `frontend/.env` 的 `VITE_API_BASE_URL`
直连本服务，因此跑起 Web 界面不再需要 `npm run build`。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.chat import router as api_router
from app.config import settings
from app.services.http_client import close_http_client


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
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

    # 前端与后端不同源（Vite 跑在自己的端口上），跨域是常态而非例外
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.get_cors_origins(),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router)

    return app


app = create_app()
