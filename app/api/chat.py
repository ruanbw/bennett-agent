"""HTTP 接口层。

本层是**唯一的序列化点**：`ChatEvent` 对象在这里变成 SSE 帧。服务层不再产出
`"data: ..."` 字符串（R-1），因此也不存在「剥掉前缀再让框架加回去」的往返。

**本模块所有端点都要求登录**（依赖 `current_active_user`）。它们要么消耗上游模型
额度，要么暴露网关可用模型清单，都不应该匿名可访。认证接口本身在 `app/api/auth.py`。
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

#: 关闭中间层缓冲，否则流式输出会被反向代理攒成一整块。
#: `Cache-Control` 用 `no-store`（与库默认一致）：SSE 是即时的、不可复用的一次性
#: 响应，此前写的 `no-cache` 会覆盖库默认值，允许缓存先存下来再校验复用。
#: `Connection: keep-alive` 由 sse-starlette 在合并 headers 之后强制写入，
#: 在这里删是删不掉的，见 `ChatEventSourceResponse`。
SSE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Accel-Buffering": "no",
}

#: 单次 ASGI send 的超时。半开连接（客户端掉线但 TCP 未断）下 `await send` 会无限
#: 阻塞，库默认 `send_timeout=None` 即不设超时，这条连接会一直占着上游额度。
SSE_SEND_TIMEOUT_SECONDS = 30.0

#: 对话限流桶。按**账号**计数，与认证端点的 IP 桶分开。
_limiter: object | None = None


class ChatEventSourceResponse(EventSourceResponse):
    """剥掉 sse-starlette 强制写入的 `Connection: keep-alive`。

    库在合并调用方 headers **之后**仍无条件 `_headers["Connection"] = "keep-alive"`
    （sse_starlette 3.5.0 sse.py:317），没有开关可关。该标头只属于 HTTP/1.1，
    RFC 7540 §8.1.2.2 明确禁止 HTTP/2 响应携带它——网关可能因此重置流。
    ASGI 实际发送的是 `raw_headers`，所以必须在 `__init__` 之后从那里删；
    只改 `SSE_HEADERS` 或 `self.headers` 都拦不住（库早已把它写进 raw_headers）。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        # 所有 SSE 响应都拿到半开连接兜底，避免调用点漏传。
        kwargs.setdefault("send_timeout", SSE_SEND_TIMEOUT_SECONDS)
        super().__init__(*args, **kwargs)

        self.raw_headers = [(name, value) for name, value in self.raw_headers if name.lower() != b"connection"]
        # `Response.headers` 会缓存一个持有旧列表的 MutableHeaders；丢掉缓存让它按
        # 新的 raw_headers 重建，否则外部从这里还能读到已删除的标头。
        self.__dict__.pop("_headers", None)


def get_chat_limiter():  # noqa: ANN201
    """取对话限流器；配额/后端变化时重建（让测试改 settings 后立即生效）。"""
    global _limiter
    _limiter = make_limiter(_limiter, max(settings.chat_rate_limit_per_minute, 1))
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

    allowed, retry_after = await get_chat_limiter().acheck(f"user:{user.id}")
    if not allowed:
        # limit 显式传入：限流器不是全局单例，too_many 自己拿不到配额，
        # 不传就不会发 X-RateLimit-Limit（认证端点已有，这里此前漏了）
        raise too_many(retry_after, limit=get_chat_limiter().limit)


def to_sse_frame(event: ChatEvent) -> ServerSentEvent:
    """把聊天事件编码成 SSE 帧。

    事件对象 → 帧的转换只在这里发生一次。`ServerSentEvent` 负责加 `data: ` 前缀与
    结尾空行，并用 CRLF 分隔；前端按 `\\r?\\n\\r?\\n` 分帧即可。
    """
    return ServerSentEvent(data=event.model_dump_json())


async def resolve_request_model(model: str | None) -> ChatModelSnapshot:
    """解析本轮实际使用的模型：白名单 + 目标 + 窗口上限，一次读库。

    客户端不传 `model` 时取后台默认模型；数据库已配置但没有任何启用模型时返回 503——
    不能回退到 `MODEL_NAME`，那会让「管理员停用了全部模型」看起来像是没生效。

    白名单（`known_ids`）：显式传入的 model 必须是网关真实暴露的 id。没有这一道，
    登录用户能拿服务端的 key 去调网关没暴露（或更贵）的模型——`model` 只是转发给上游
    的字符串。旧版网关兜底时不再返回 `None`（放行任意模型名），而是收敛为配置里的
    模型名；`None` 只保留给数据库路径的「无法校验」语义。

    数据库路径下「这个 key 不在启用模型里」不会在这里 400：`target=None` 留给对话流
    以 SSE 的 `model_unavailable` 收尾（流已经开了，客户端拿得到可展示的失败原因）。
    这里只做「显式传了一个不在白名单里的 key」这一种 400。
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
    """请求的思考等级必须在该模型声明的等级内（模型没声明时无法校验，放行）。

    `"none"` 是「关闭思考」的归一化结果（前端用 off/false 表达），始终允许：
    很多网关把它当作独立的开关，而不是 `supported_reasoning_levels` 里的一项。
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
    """逐字返回思考过程、工具调用与生成内容。

    客户端无状态上传完整会话历史；服务端不保存任何会话状态。
    事件以 `done` 或 `error` 收尾。
    """
    snapshot = await resolve_request_model(request.model)
    validate_reasoning_effort(snapshot, request.reasoning_effort)

    async def event_generator():
        async for event in stream_chat_events(
            snapshot,
            request.messages,
            reasoning_effort=request.reasoning_effort,
            context_anchor=request.context_anchor,
            cache_scope_key=str(user.id),
            # 知识库是用户私有的：工具只能查当前登录用户的文档（见
            # `app/tools/knowledge_scope.py`）。这个值由服务端注入，绝不来自模型参数。
            user_id=user.id,
            # 会话标识：沙箱是「每会话一台机器」，服务端需要它定位租约。它只是**标识**，
            # 不是授权凭据（见 `app/tools/sandbox_scope.py`）：即便被谎报，
            # 租约查询也一律带 user_id，拿到的只能是本用户自己的机器。
            session_id=request.session_id,
            # 审批续跑：客户端带回「批准/拒绝」。是否真的续跑由服务层看历史形状决定
            # （历史以带工具调用的 assistant 结尾才算），这里原样透传。
            approvals=request.approvals,
        ):
            yield to_sse_frame(event)

    return ChatEventSourceResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
