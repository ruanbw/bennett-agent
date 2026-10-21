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
from fastapi.openapi.utils import get_openapi

from app.api.admin import router as admin_router
from app.api.auth import router as auth_router
from app.api.capabilities import router as capabilities_router
from app.api.chat import router as api_router
from app.api.sync import router as sync_router
from app.config import settings
from app.db import cleanup_expired_access_tokens, create_db_and_tables, engine
from app.middleware import MaxBodySizeMiddleware, OriginGuardMiddleware, SecurityHeadersMiddleware
from app.prompts import prompt_manager
from app.services.http_client import close_http_client
from app.services.model_service import seed_default_provider
from app.version import APP_VERSION

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

    headers_configured = bool(settings.trusted_proxy_headers.strip())
    ips_configured = bool(settings.trusted_proxy_ips.strip())

    if headers_configured and not ips_configured:
        # 只配标头不配代理网段等于「谁来都信」：直连客户端可以轮换 XFF 绕过限流（L-10）。
        # 限流器会 fail closed（不采信标头），但部署者必须知道这个配置不会生效。
        logger.warning(
            "TRUSTED_PROXY_HEADERS 已配置但 TRUSTED_PROXY_IPS 为空：代理标头不会被采信，"
            "认证限流将按直连地址计数；如确实在反向代理之后，请补上 TRUSTED_PROXY_IPS。"
        )

    if headers_configured and ips_configured:
        return

    logger.warning(
        "认证限流按 client.host 计数：若本服务部署在反向代理之后，所有请求会被算作同一个来源"
        "（全站共享 %d 次/分钟）。请在代理上设置 X-Forwarded-For 并同时配置"
        " TRUSTED_PROXY_HEADERS 与 TRUSTED_PROXY_IPS，或直连后端。",
        settings.auth_rate_limit_per_minute,
    )


def validate_prompts() -> None:
    """启动自检：编译全部模板，并确认主系统提示词能渲染出内容。

    模板写坏（少一个 `endif`、变量名拼错、内容被条件分支挡光）此前的表现是：服务正常
    启动，直到某位用户发第一条消息才在预算预检里炸成「请求处理失败」——现场既看不见
    是提示词的问题，也不会有人想到去查模板文件。启动即失败比那好得多：这是一个纯文件
    问题，重启就是修复动作。

    抛 `PromptTemplateError` 由调用方决定如何呈现（lifespan 里会记 error 并中止启动）。
    """
    from app.services.agent_factory import render_system_prompt

    names = prompt_manager.validate_templates()
    # 语法通过不代表能渲染：缺变量（StrictUndefined）与「渲染成空串」都只有真渲染才暴露
    rendered = render_system_prompt()
    logger.debug("提示词自检通过：%d 个模板，系统提示词 %d 字符", len(names), len(rendered))


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # 提示词是纯文件：写坏了就立刻失败，别等第一条用户消息才炸
    try:
        validate_prompts()
    except Exception as exc:  # noqa: BLE001 - 统一转成启动失败，日志里点明原因
        logger.error("提示词自检失败，服务不会启动: %s", exc)
        raise

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


#: OpenAPI 分组说明。
#:
#: 端点本身已有中文 summary，但 /docs 左侧的分组默认只有英文 tag 名（auth/chat…），
#: 第一次打开的人分不清哪个是登录、哪个是管理。这里给每组一句用途与权限说明；
#: `tests/test_server.py` 还会锁住「没有端点落在未声明的 tag 下」，避免新增路由时忘了分组。
_API_TAGS = [
    {
        "name": "auth",
        "description": "注册、登录、登出、当前用户。会话是 HttpOnly cookie，登录接口用表单编码。",
    },
    {
        "name": "chat",
        "description": "模型列表、SSE 对话与标题生成。`POST /api/chat` 返回 `text/event-stream`。",
    },
    {
        "name": "capabilities",
        "description": "运行时能力探针：报告实际装配的工具（如是否可联网搜索）。",
    },
    {
        "name": "sync",
        "description": "本机数据增量同步：push 逐行接受或拒绝，pull 用不透明游标做长轮询。",
    },
    {
        "name": "admin",
        "description": "提供商 / 模型 / 用户管理。需要 admin 及以上；角色调整仅 superadmin。",
    },
]


#: 不需要登录、因而不该在文档里标「需要 cookie 会话」的端点。
#:
#: 这份清单是**刻意显式**的：新增一个公开端点时必须改这里，`tests/test_server.py` 会
#: 逼你做这个决定，而不是悄悄多出一个没有任何认证说明的接口。
_PUBLIC_PATHS = frozenset({"/api/auth/register", "/api/auth/login"})

#: OpenAPI 里的 HTTP 方法（`paths` 下还有 `parameters` 这类非方法键，必须过滤掉）。
_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})


def _install_cookie_security_scheme(app: FastAPI) -> None:
    """在 OpenAPI 里声明「会话是一个 cookie」。

    此前文档只在描述里写着「要求 HttpOnly cookie 会话」：`/docs` 里没有 Authorize 入口，
    也没法一眼看出哪些端点要登录、登录后浏览器会自动带哪个 cookie。声明成
    `apiKey in: cookie` 之后，Swagger UI 会把需要认证的端点标出来，客户端生成器也能
    据此知道凭据放在哪里。

    公开端点（注册/登录/健康检查）**不加** `security`：给它们标上认证要求会让客户端
    生成器在登录前就去要凭据，形成死循环。
    """

    def custom_openapi() -> dict:
        if app.openapi_schema is not None:
            return app.openapi_schema

        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
            tags=_API_TAGS,
        )
        schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
        # fastapi-users 已经声明了 `APIKeyCookie`（`in: cookie`，名字取自 transport）。
        # 再补一个等价的 `cookieAuth` 只会让文档里出现两个意义相同的方案，客户端生成器
        # 还要用户二选一——复用它，只把说明补成中文。
        scheme_name = "APIKeyCookie"
        scheme = schemes.setdefault(
            scheme_name,
            {
                "type": "apiKey",
                "in": "cookie",
                "name": settings.auth_cookie_name,
            },
        )
        scheme["description"] = (
            "登录成功后由服务端写入的 HttpOnly 会话 cookie（不透明 token，非 JWT）。"
            "浏览器会自动携带；用 curl 调试时需要 `-c/-b` 保存与回传。"
        )

        for path, methods in schema.get("paths", {}).items():
            if path in _PUBLIC_PATHS:
                continue
            for method, operation in methods.items():
                if method.lower() not in _HTTP_METHODS:
                    continue
                operation["security"] = [{scheme_name: []}]

        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


def create_app() -> FastAPI:
    docs_enabled = _api_docs_enabled()
    app = FastAPI(
        title=f"{settings.assistant_name} Agent API",
        description=(
            "基于 FastAPI + PydanticAI + LiteLLM 的对话助手服务。\n\n"
            "除注册与登录外，所有 `/api/*` 都要求 HttpOnly cookie 会话；"
            "接口文档默认只在非 production 环境暴露，可用 `ENABLE_API_DOCS=true` 显式打开。"
        ),
        # 版本号只有一个来源（已安装包的元数据），不再与 pyproject 里的值各说各话
        version=APP_VERSION,
        openapi_tags=_API_TAGS,
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    _install_cookie_security_scheme(app)

    # 前端与后端不同源（Vite 跑在自己的端口上），跨域是常态而非例外
    cors_origins = settings.get_cors_origins()
    if settings.cors_allow_credentials and "*" in cors_origins:
        # 浏览器对「通配来源 + 携带凭据」的响应一律拒绝，静默降级成跨域不可用
        # 比启动即报错难查得多；这里要求把来源写成具体域名。
        raise ValueError(
            'CORS 配置无效：cors_allow_credentials=True 时 cors_allowed_origins 不能包含 "*"，'
            "浏览器会拒绝该组合；请改成具体来源（如 https://app.example.com）。"
        )

    # 请求体上限 / 跨站请求守卫必须在 CORS **之前**注册（后注册的更外层），
    # 这样 413/403 响应同样带上 CORS 头，跨域前端能读到具体错误。
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(OriginGuardMiddleware, allowed_origins=cors_origins)

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

    # 最后注册 = 最外层：连 CORS/限流产生的错误响应也带上安全头。
    app.add_middleware(SecurityHeadersMiddleware)

    app.include_router(auth_router)
    app.include_router(api_router)
    app.include_router(capabilities_router)
    app.include_router(admin_router)
    app.include_router(sync_router)

    return app


app = create_app()
