"""HTTP 接口层。

本层是**唯一的序列化点**：`ChatEvent` 对象在这里变成 SSE 帧。服务层不再产出
`"data: ..."` 字符串（R-1），因此也不存在「剥掉前缀再让框架加回去」的往返。
"""

from math import ceil

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from app.api.rate_limit import SlidingWindowLimiter, client_key, resolve_client_ip
from app.config import settings
from app.schemas.chat import (
    ChatRequest,
    ModelListResponse,
    TitleGenerateRequest,
    TitleGenerateResponse,
)
from app.schemas.events import ChatEvent
from app.services.chat_service import generate_title_from_prompt, stream_chat_events
from app.services.model_service import fetch_available_models

router = APIRouter(prefix="/api")

#: 关闭中间层缓冲，否则流式输出会被反向代理攒成一整块。
#: 遵循 RFC 7540 HTTP/2 规范，避免向 HTTP/2 响应设置特定于连接的标头 Connection: keep-alive。
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}

#: 限流窗口长度（秒）。
RATE_LIMIT_WINDOW_SECONDS = 60.0

_limiter: SlidingWindowLimiter | None = None


def get_chat_limiter() -> SlidingWindowLimiter:
    """取进程内限流器；配置变化时重建。

    惰性创建而非模块级常量，是为了让测试能改 `settings` 后立即生效。
    """
    global _limiter
    limit = max(settings.chat_rate_limit_per_minute, 1)
    if _limiter is None or _limiter.limit != limit:
        _limiter = SlidingWindowLimiter(limit=limit, window_seconds=RATE_LIMIT_WINDOW_SECONDS)
    return _limiter


def get_trusted_proxy_headers() -> list[str]:
    """获取配置中受信的代理标头列表。"""
    raw = settings.trusted_proxy_headers
    if not raw or not raw.strip():
        return []
    return [h.strip() for h in raw.split(",") if h.strip()]


async def enforce_chat_rate_limit(request: Request) -> None:
    """对话接口的限流依赖（C-3）。

    进程内实现，挡的是「单个来源猛打」；精确配额需要共享存储，见 `rate_limit.py`。
    支持反向代理 IP 识别（需在 settings 中配置 trusted_proxy_headers）。
    """
    if settings.chat_rate_limit_per_minute <= 0:
        return

    direct_host = request.client.host if request.client else None
    trusted_headers = get_trusted_proxy_headers()
    ip = resolve_client_ip(direct_host, headers=dict(request.headers), trusted_headers=trusted_headers)

    allowed, retry_after = get_chat_limiter().check(client_key(ip))
    if allowed:
        return

    seconds = max(1, ceil(retry_after))
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=f"请求过于频繁，请在 {seconds} 秒后重试",
        headers={"Retry-After": str(seconds)},
    )


def to_sse_frame(event: ChatEvent) -> ServerSentEvent:
    """把聊天事件编码成 SSE 帧。

    事件对象 → 帧的转换只在这里发生一次。`ServerSentEvent` 负责加 `data: ` 前缀与
    结尾空行，并用 CRLF 分隔；前端按 `\\r?\\n\\r?\\n` 分帧即可。
    """
    return ServerSentEvent(data=event.model_dump_json())


@router.get("/models", response_model=ModelListResponse, summary="获取可用大模型列表")
async def get_models():
    """获取系统支持的所有大模型列表，包含是否支持思考链、上下文窗口等元数据。"""
    return await fetch_available_models()


@router.post(
    "/chat/title",
    response_model=TitleGenerateResponse,
    summary="根据提示词意向生成会话标题",
    dependencies=[Depends(enforce_chat_rate_limit)],
)
async def generate_title(request: TitleGenerateRequest):
    """提取首条用户提示词的意向，调用模型生成极简会话标题"""
    model_to_use = request.model or settings.model_name
    title = await generate_title_from_prompt(model_to_use, request.prompt)
    return TitleGenerateResponse(title=title)


@router.post(
    "/chat",
    summary="对话接口（SSE 流式输出）",
    dependencies=[Depends(enforce_chat_rate_limit)],
)
async def chat_stream(request: ChatRequest):
    """逐字返回思考过程、工具调用与生成内容。

    客户端无状态上传完整会话历史；服务端不保存任何会话状态。
    事件以 `done` 或 `error` 收尾。
    """
    model_to_use = request.model or settings.model_name

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
