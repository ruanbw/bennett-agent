import json
from typing import AsyncGenerator, List
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    UserPromptPart,
    TextPart,
    ThinkingPart,
)
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.litellm import LiteLLMProvider

from app.config import settings
from app.prompts import prompt_manager
from app.schemas.chat import ChatMessage
from app.tools import ALL_TOOLS


def get_agent_for_model(model_name: str) -> Agent:
    """动态根据前端选中的 model_name 装配 Agent"""
    system_prompt = prompt_manager.render(
        "system_prompt.jinja",
        app_env=settings.app_env,
        assistant_name=settings.assistant_name,
    )

    provider = LiteLLMProvider(
        api_base=settings.litellm_api_base,
        api_key=settings.litellm_api_key,
    )
    model = OpenAIChatModel(
        model_name=model_name,
        provider=provider,
    )

    return Agent(
        model=model,
        system_prompt=system_prompt,
        tools=ALL_TOOLS,
    )


def convert_chat_history(messages: List[ChatMessage]) -> tuple[str, List[ModelMessage]]:
    """将前端上传的历史对话转换为 PydanticAI 所需的 (prompt, message_history)"""
    if not messages:
        raise ValueError("消息列表不能为空")

    # 取最后一条 user 消息作为当前轮 prompt
    last_msg = messages[-1]
    if last_msg.role != "user":
        # 兼容处理
        current_prompt = last_msg.content
        history_msgs = messages[:-1]
    else:
        current_prompt = last_msg.content
        history_msgs = messages[:-1]

    converted_history: List[ModelMessage] = []
    for msg in history_msgs:
        if msg.role == "user":
            converted_history.append(
                ModelRequest(parts=[UserPromptPart(content=msg.content)])
            )
        elif msg.role == "assistant":
            parts = []
            if msg.thinking:
                parts.append(ThinkingPart(content=msg.thinking))
            parts.append(TextPart(content=msg.content))
            converted_history.append(
                ModelResponse(parts=parts)
            )

    return current_prompt, converted_history


async def stream_chat_response(
    model_name: str,
    messages: List[ChatMessage]
) -> AsyncGenerator[str, None]:
    """通过 SSE 逐字向前端推送流式回复，支持思考过程 (thinking) 与最终内容 (content)"""
    try:
        current_prompt, history = convert_chat_history(messages)
    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
        return

    try:
        agent = get_agent_for_model(model_name)
    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'message': f'模型初始化失败: {str(e)}'}, ensure_ascii=False)}\n\n"
        return

    try:
        last_think_len = 0
        last_text_len = 0

        async with agent.run_stream(current_prompt, message_history=history) as res:
            async for resp in res.stream_response():
                for part in resp.parts:
                    if isinstance(part, ThinkingPart):
                        content = part.content or ""
                        if len(content) > last_think_len:
                            delta = content[last_think_len:]
                            last_think_len = len(content)
                            data = json.dumps({"type": "thinking", "delta": delta}, ensure_ascii=False)
                            yield f"data: {data}\n\n"
                    elif isinstance(part, TextPart):
                        content = part.content or ""
                        if len(content) > last_text_len:
                            delta = content[last_text_len:]
                            last_text_len = len(content)
                            data = json.dumps({"type": "content", "delta": delta}, ensure_ascii=False)
                            yield f"data: {data}\n\n"

        yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"

    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'message': f'生成异常: {str(e)}'}, ensure_ascii=False)}\n\n"
