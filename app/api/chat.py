import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from app.config import settings
from app.schemas.chat import ChatMessage, ChatRequest, ModelListResponse
from app.services.chat_service import stream_chat_response
from app.services.model_service import fetch_available_models

router = APIRouter(prefix="/api")


@router.get("/models", response_model=ModelListResponse, summary="获取可用大模型列表")
async def get_models():
    """获取系统支持的所有大模型列表，包含是否支持思考链、上下文窗口等元数据。"""
    return await fetch_available_models()


async def _collect_chat_response(
    model_name: str,
    messages: list[ChatMessage],
) -> dict:
    """stream=false 时聚合流式增量，一次性返回完整结果。"""
    content_parts: list[str] = []
    thinking_parts: list[str] = []

    async for chunk in stream_chat_response(model_name, messages):
        payload = chunk.strip()
        if payload.startswith("data:"):
            payload = payload[len("data:"):].strip()
        if not payload:
            continue

        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")
        if event_type == "content":
            content_parts.append(event.get("delta", ""))
        elif event_type == "thinking":
            thinking_parts.append(event.get("delta", ""))
        elif event_type == "error":
            raise HTTPException(status_code=502, detail=event.get("message", "生成失败"))

    return {
        "content": "".join(content_parts),
        "thinking": "".join(thinking_parts) or None,
    }


@router.post("/chat", summary="对话接口（stream=true 时 SSE 流式输出）")
async def chat_stream(request: ChatRequest):
    """支持客户端无状态上传完整会话历史；stream=true 逐字返回思考过程与生成内容。"""
    model_to_use = request.model or settings.model_name

    if not request.stream:
        return await _collect_chat_response(
            model_to_use, request.messages
        )

    return StreamingResponse(
        stream_chat_response(
            model_to_use, request.messages
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Content-Type": "text/event-stream; charset=utf-8",
        },
    )
