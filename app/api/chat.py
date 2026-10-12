"""HTTP 接口层。

本层是**唯一的序列化点**：`ChatEvent` 对象在这里变成 SSE 帧。服务层不再产出
`"data: ..."` 字符串（R-1），因此也不存在「剥掉前缀再让框架加回去」的往返。

**本模块所有端点都要求登录**（依赖 `current_active_user`）。它们要么消耗上游模型
额度，要么暴露网关可用模型清单，都不应该匿名可访。认证接口本身在 `app/api/auth.py`。
"""

from fastapi import APIRouter, Depends
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from app.api.rate_limit import SlidingWindowLimiter, limiter_with, too_many
from app.config import settings
from app.db import User
from app.schemas.chat import (
    ChatRequest,
    ModelListResponse,
    TitleGenerateRequest,
    TitleGenerateResponse,
)
from app.schemas.events import ChatEvent
from app.services.auth import current_active_user
from app.services.chat_service import generate_title_from_prompt, stream_chat_events
from app.services.model_service import fetch_available_models

router = APIRouter(prefix="/api")

#: 关闭中间层缓冲，否则流式输出会被反向代理攒成一整块。
#: 遵循 RFC 7540 HTTP/2 规范，避免向 HTTP/2 响应设置特定于连接的标头 Connection: keep-alive。
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}

#: 对话限流桶。按**账号**计数，与认证端点的 IP 桶分开。
_limiter: SlidingWindowLimiter | None = None


def get_chat_limiter() -> SlidingWindowLimiter:
    """取对话限流器；配额变化时重建（让测试改 `settings` 后立即生效）。"""
    global _limiter
    _limiter = limiter_with(_limiter, max(settings.chat_rate_limit_per_minute, 1))
    return _limiter


async def enforce_chat_rate_limit(user: User = Depends(current_active_user)) -> None:  # noqa: B008
    """对话接口的限流依赖（C-3）：按**账号**计数。

    改动前按 IP，但两件事让它失效：

    1. 同源代理（开发期 Vite、生产 Nginx）下，后端看到的 `client.host` 永远是
       代理地址，配额从「每 IP」退化成「全站」——一个人打满就挡住所有人；
    2. IP 是共享的（NAT、公司出口），无关用户会互相挤占配额。

    端点已经要求登录，账号就是天然且稳定的限流键。IP 维度的限流留给认证端点
    （`app/api/rate_limit.py:enforce_auth_rate_limit`）——那里才是它真正有用的地方。

    本函数**声明了** `current_active_user` 依赖，所以只要它被使用，认证就一定会先生效：
    即使配额配成 0 而提前 return，登录要求依然成立。
    """
    if settings.chat_rate_limit_per_minute <= 0:
        return

    allowed, retry_after = get_chat_limiter().check(f"user:{user.id}")
    if not allowed:
        raise too_many(retry_after)


def to_sse_frame(event: ChatEvent) -> ServerSentEvent:
    """把聊天事件编码成 SSE 帧。

    事件对象 → 帧的转换只在这里发生一次。`ServerSentEvent` 负责加 `data: ` 前缀与
    结尾空行，并用 CRLF 分隔；前端按 `\\r?\\n\\r?\\n` 分帧即可。
    """
    return ServerSentEvent(data=event.model_dump_json())


@router.get(
    "/models",
    response_model=ModelListResponse,
    summary="获取可用大模型列表",
    dependencies=[Depends(current_active_user)],
)
async def get_models():
    """获取系统支持的所有大模型列表，包含是否支持思考链、上下文窗口等元数据。"""
    return await fetch_available_models()


@router.post(
    "/chat/title",
    response_model=TitleGenerateResponse,
    summary="根据提示词意向生成会话标题",
    dependencies=[Depends(current_active_user), Depends(enforce_chat_rate_limit)],
)
async def generate_title(request: TitleGenerateRequest):
    """提取首条用户提示词的意向，调用模型生成极简会话标题"""
    model_to_use = (request.model.strip() if request.model else "") or settings.model_name
    title = await generate_title_from_prompt(model_to_use, request.prompt)
    return TitleGenerateResponse(title=title)


@router.post(
    "/chat",
    summary="对话接口（SSE 流式输出）",
    dependencies=[Depends(current_active_user), Depends(enforce_chat_rate_limit)],
)
async def chat_stream(request: ChatRequest):
    """逐字返回思考过程、工具调用与生成内容。

    客户端无状态上传完整会话历史；服务端不保存任何会话状态。
    事件以 `done` 或 `error` 收尾。
    """
    model_to_use = (request.model.strip() if request.model else "") or settings.model_name

    async def event_generator():
        async for event in stream_chat_events(
            model_to_use,
            request.messages,
            reasoning_effort=request.reasoning_effort,
        ):
            yield to_sse_frame(event)

    return EventSourceResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
