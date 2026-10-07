from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import os

from app.api.chat import router as api_router
from app.config import settings


def create_app() -> FastAPI:
    app = FastAPI(
        title=f"{settings.assistant_name} Agent API",
        description="基于 FastAPI + PydanticAI + LiteLLM 的对话助手服务",
        version="1.0.0",
    )

    # 允许跨域请求（移除敏感且不符合安全规范的 allow_credentials=True 全通配）
    cors_origins = settings.get_cors_origins()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册 API 路由
    app.include_router(api_router)

    # 如果前端已构建出 dist 目录，直接挂载静态文件
    frontend_dist = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
    if os.path.exists(frontend_dist):
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")

    return app


app = create_app()
