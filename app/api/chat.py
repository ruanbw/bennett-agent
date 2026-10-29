"""HTTP 接口层：`ChatEvent` 在这里序列化成 SSE 帧，是唯一的序列化点。
所有端点都要求登录（它们消耗上游额度或暴露模型清单），认证接口在 `app/api/auth.py`。
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse, ServerSentEvent

from app.api.rate_limit import make_limiter, too_many
from app.config import settings
from app.db import User
from app.schemas.chat import (
    ChatRequest,
    ModelListResponse,
    TitleGenerateRequest,
    TitleGenerateResponse,
)
from app.schemas.events import ChatEvent
from app.services.agent_factory import normalize_reasoning_effort
from app.services.auth import current_active_user
from app.services.chat_service import generate_title_from_prompt, stream_chat_events
from app.services.model_service import ChatModelSnapshot, fetch_available_models, resolve_chat_model

router = APIRouter(prefix="/api", tags=["chat"])

#: `X-Accel-Buffering: no` 关掉中间层缓冲，否则流式输出会被反向代理攒成一整块；
#: `no-store` 沿用库默认（`no-cache` 允许缓存先存下来复用）；`Connection` 在这里删不掉，见 `ChatEventSourceResponse`。
SSE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Accel-Buffering": "no",
}

#: 单次 ASGI send 超时：半开连接下 `await send` 会无限阻塞（库默认不设超时），一直占着上游额度。
SSE_SEND_TIMEOUT_SECONDS = 30.0

#: 对话限流桶：按账号计数，与认证端点的 IP 桶分开。
_limiter: object | None = None


class ChatEventSourceResponse(EventSourceResponse):
    """剥掉 sse-starlette 强制写入的 `Connection: keep-alive`（HTTP/2 禁止该标头，网关会重置流）。
    库在合并 headers 之后仍无条件写入（3.5.0 sse.py:317），只能从 `raw_headers` 删；改 `SSE_HEADERS` 拦不住。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        # 所有 SSE 响应都拿到半开连接兜底，避免调用点漏传。
        kwargs.setdefault("send_timeout", SSE_SEND_TIMEOUT_SECONDS)
        super().__init__(*args, **kwargs)

        self.raw_headers = [(name, value) for name, value in self.raw_headers if name.lower() != b"connection"]
        # `Response.headers` 缓存了持有旧列表的 MutableHeaders，丢掉缓存才会按新 raw_headers 重建。
        self.__dict__.pop("_headers", None)


def get_chat_limiter():  # noqa: ANN201
    """取对话限流器；配额/后端变化时重建（让测试改 settings 后立即生效）。"""
    global _limiter
    _limiter = make_limiter(_limiter, max(settings.chat_rate_limit_per_minute, 1))
    return _limiter


async def enforce_chat_rate_limit(user: User = Depends(current_active_user)) -> None:  # noqa: B008
    """对话接口的限流依赖：按账号计数（IP 在代理后只剩代理地址，配额会退化成全站，NAT 用户还会互相挤占）。
    IP 维度留给认证端点（见 `app/api/rate_limit.py`）；本函数声明了 `current_active_user`，配额为 0 提前
    return 时登录要求依然成立。
    """
    if settings.chat_rate_limit_per_minute <= 0:
        return

    allowed, retry_after = await get_chat_limiter().acheck(f"user:{user.id}")
    if not allowed:
        # 限流器不是全局单例，too_many 自己拿不到配额，不显式传就不会发 X-RateLimit-Limit。
        raise too_many(retry_after, limit=get_chat_limiter().limit)


def to_sse_frame(event: ChatEvent) -> ServerSentEvent:
    """把聊天事件编码成 SSE 帧：`ServerSentEvent` 负责加 `data: ` 前缀与结尾空行。
    前端按 `\\r?\\n\\r?\\n` 分帧即可，帧转换只在这里发生一次。
    """
    return ServerSentEvent(data=event.model_dump_json())


async def resolve_request_model(model: str | None) -> ChatModelSnapshot:
    """解析本轮实际使用的模型：白名单 + 目标 + 窗口上限，一次读库。
    不传 model 取后台默认；一个启用模型都没有时返回 503 而不回退 `MODEL_NAME`（否则停用全部模型看起来像没生效）；
    显式 model 必须在 `known_ids` 里（`model` 只是转发给上游的字符串，不校验就能用服务端 key 调任意模型）。
    """
    snapshot = await resolve_chat_model(model)

    if model:
        if snapshot.known_ids is not None and model not in snapshot.known_ids:
            raise HTTPException(status_code=400, detail=f"模型 {model} 不在可用模型列表中，请从模型列表中选择。")
        return snapshot

    if not snapshot.key:
        raise HTTPException(status_code=503, detail="管理员尚未启用任何模型，请联系管理员。")
    return snapshot


def validate_reasoning_effort(snapshot: ChatModelSnapshot, reasoning_effort: str | None) -> None:
    """请求的思考等级必须在该模型声明的等级内，模型没声明时放行。
    `"none"`（前端 off/false 的归一化结果）始终允许：多数网关把它当独立开关而非等级项。
    """
    if not reasoning_effort or not snapshot.reasoning_levels:
        return

    normalized = normalize_reasoning_effort(reasoning_effort)
    if normalized is None or normalized == "none":
        return
    if normalized not in snapshot.reasoning_levels:
        levels = "、".join(sorted(snapshot.reasoning_levels))
        raise HTTPException(
            status_code=400,
            detail=f"模型 {snapshot.key} 不支持思考等级 {reasoning_effort}，可选等级：{levels}。",
        )


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
    snapshot = await resolve_request_model(request.model)
    title = await generate_title_from_prompt(snapshot, request.prompt)
    return TitleGenerateResponse(title=title)


@router.post(
    "/chat",
    summary="对话接口（SSE 流式输出）",
    dependencies=[Depends(current_active_user), Depends(enforce_chat_rate_limit)],
)
async def chat_stream(request: ChatRequest, user: User = Depends(current_active_user)):  # noqa: B008
    """逐字返回思考过程、工具调用与生成内容；客户端无状态上传完整历史，服务端不留会话状态。"""
    snapshot = await resolve_request_model(request.model)
    validate_reasoning_effort(snapshot, request.reasoning_effort)

    async def event_generator():
        async for event in stream_chat_events(
            snapshot,
            request.messages,
            reasoning_effort=request.reasoning_effort,
            context_anchor=request.context_anchor,
            cache_scope_key=str(user.id),
            # 沙箱租约归属与工具缓存的按人隔离依据；由服务端注入，绝不来自模型参数。
            user_id=user.id,
            # 会话标识只用来标记「最后是谁在用」，不是授权凭据：租约查询一律带 user_id，
            # 谎报 session_id 也只能拿到本用户自己的机器（见 `app/tools/sandbox_scope.py`）。
            session_id=request.session_id,
            # 批准/拒绝原样透传；是否真的续跑由服务层看历史形状决定（以带工具调用的 assistant 结尾）。
            approvals=request.approvals,
        ):
            yield to_sse_frame(event)

    return ChatEventSourceResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
