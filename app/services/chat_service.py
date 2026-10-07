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


def get_agent_for_model(model_name: str, reasoning_effort: str | None = None) -> Agent:
    system_prompt = prompt_manager.render(
        "system_prompt.jinja",
        app_env=settings.app_env,
        assistant_name=settings.assistant_name,
    )

    provider = LiteLLMProvider(
        api_base=settings.litellm_api_base,
        api_key=settings.litellm_api_key,
    )

    model_settings = {}
    if reasoning_effort:
        # 如果明确设置为 off 或 none，则关闭思考
        effort_val = "none" if reasoning_effort in ("off", "none", "false") else reasoning_effort
        model_settings["extra_body"] = {
            "reasoning_effort": effort_val,
        }

    model = OpenAIChatModel(
        model_name=model_name,
        provider=provider,
        settings=model_settings or None,
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
    messages: List[ChatMessage],
    reasoning_effort: str | None = None,
) -> AsyncGenerator[str, None]:
    """通过 SSE 逐字向前端推送流式回复，支持思考过程 (thinking)、工具调用 (tool) 与最终内容 (content)"""
    try:
        current_prompt, history = convert_chat_history(messages)
    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
        return

    try:
        agent = get_agent_for_model(model_name, reasoning_effort=reasoning_effort)
    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'message': f'模型初始化失败: {str(e)}'}, ensure_ascii=False)}\n\n"
        return

    try:
        async with agent.run_stream_events(current_prompt, message_history=history) as stream:
            async for event in stream:
                ev_type = type(event).__name__
                if ev_type == "PartDeltaEvent":
                    delta = getattr(event, "delta", None)
                    delta_type = type(delta).__name__
                    if delta_type == "ThinkingPartDelta":
                        content = getattr(delta, "content_delta", "")
                        if content:
                            yield f"data: {json.dumps({'type': 'thinking', 'delta': content}, ensure_ascii=False)}\n\n"
                    elif delta_type == "TextPartDelta":
                        content = getattr(delta, "content_delta", "")
                        if content:
                            yield f"data: {json.dumps({'type': 'content', 'delta': content}, ensure_ascii=False)}\n\n"

                elif ev_type == "PartStartEvent":
                    part = getattr(event, "part", None)
                    if isinstance(part, TextPart) and part.content:
                        yield f"data: {json.dumps({'type': 'content', 'delta': part.content}, ensure_ascii=False)}\n\n"
                    elif isinstance(part, ThinkingPart) and part.content:
                        yield f"data: {json.dumps({'type': 'thinking', 'delta': part.content}, ensure_ascii=False)}\n\n"

                elif ev_type == "FunctionToolCallEvent":
                    part = getattr(event, "part", None)
                    if part:
                        args = part.args
                        if isinstance(args, dict):
                            args_str = json.dumps(args, ensure_ascii=False)
                        else:
                            args_str = str(args or "")
                        data = {
                            "type": "tool_call",
                            "tool_call_id": getattr(part, "tool_call_id", ""),
                            "tool_name": getattr(part, "tool_name", ""),
                            "args": args_str,
                        }
                        yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

                elif ev_type == "FunctionToolResultEvent":
                    part = getattr(event, "part", None)
                    if part:
                        content = part.content
                        if not isinstance(content, str):
                            content = json.dumps(content, ensure_ascii=False)
                        data = {
                            "type": "tool_result",
                            "tool_call_id": getattr(part, "tool_call_id", ""),
                            "tool_name": getattr(part, "tool_name", ""),
                            "result": content,
                        }
                        yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

        yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"

    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'message': f'生成异常: {str(e)}'}, ensure_ascii=False)}\n\n"


async def generate_title_from_prompt(model_name: str, prompt: str) -> str:
    """根据用户输入的提示词意向，利用 LLM 快速生成简短概括的会话标题（10个字以内）"""
    system_instruction = (
        "你是一个会话标题生成助手。根据用户的问题或指令，概括出极简的会话标题。"
        "要求：1. 紧扣核心意向；2. 不超过10个汉字或5个英文单词；3. 不要包含任何标点符号、引号或前缀解释；4. 纯文本直接返回标题。"
    )

    provider = LiteLLMProvider(
        api_base=settings.litellm_api_base,
        api_key=settings.litellm_api_key,
    )

    model = OpenAIChatModel(
        model_name=model_name,
        provider=provider,
    )

    agent = Agent(
        model=model,
        system_prompt=system_instruction,
    )

    try:
        result = await agent.run(f"请为以下内容生成简短标题：\n{prompt}")
        title = result.output.strip().strip('"\'“”‘’')
        return title[:20] if title else "新对话"
    except Exception:
        # 回退逻辑
        cleaned = prompt.strip().split("\n")[0][:15]
        return cleaned or "新对话"

