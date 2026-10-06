import asyncio
import sys
from typing import List

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.litellm import LiteLLMProvider

from app.config import settings
from app.prompts import prompt_manager
from app.tools import ALL_TOOLS


# =====================================================================
# 1. 工厂方法：装配 Agent (提示词、LiteLLM 模型与工具解耦组合)
# =====================================================================
def create_agent(enable_web_search: bool = True) -> Agent:
    # 渲染动态系统提示词
    system_prompt = prompt_manager.render(
        "system_prompt.jinja",
        app_env=settings.app_env,
        assistant_name=settings.assistant_name,
    )

    # 基于 LiteLLM 构建模型适配层
    provider = LiteLLMProvider(
        api_base=settings.litellm_api_base,
        api_key=settings.litellm_api_key,
    )
    model = OpenAIChatModel(
        model_name=settings.model_name,
        provider=provider,
    )

    tools = ALL_TOOLS if enable_web_search else []

    # 统一传入解耦的工具列表
    return Agent(
        model=model,
        system_prompt=system_prompt,
        tools=tools,
    )


# =====================================================================
# 2. 交互式 Agent Loop（支持流式 Streaming 输出与多轮历史）
# =====================================================================
async def run_interactive_loop(agent: Agent):
    """运行终端交互式 Agent Loop，并以流式输出展示。"""
    print("=" * 60)
    print(" Bennett Agent 启动成功 (流式响应模式)")
    print(f" [环境] APP_ENV:          {settings.app_env}")
    print(f" [模型] MODEL_NAME:       {settings.model_name}")
    print(f" [接口] LITELLM_API_BASE: {settings.litellm_api_base}")
    print("=" * 60)
    print("提示: 输入你的问题（输入 'exit' 或 'quit' 退出，输入 'clear' 重置上下文）\n")

    message_history: List[ModelMessage] = []

    while True:
        try:
            user_input = input("User > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n退出 Agent。")
            break

        if not user_input:
            continue

        if user_input.lower() in ("exit", "quit", "q"):
            print("再见！")
            break

        if user_input.lower() == "clear":
            message_history.clear()
            print("[上下文已重置]\n")
            continue

        print("\nAgent > ", end="", flush=True)

        try:
            # 使用 run_stream 实现流式输出
            async with agent.run_stream(user_input, message_history=message_history) as response:
                async for delta in response.stream_text(delta=True):
                    sys.stdout.write(delta)
                    sys.stdout.flush()

                # 将本轮消息追加到历史上下文
                message_history = response.all_messages()

            print("\n")

        except Exception as err:
            print(f"\n[执行异常]: {err}\n")


# =====================================================================
# 3. 启动模式
# =====================================================================
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Bennett Agent 运行入口")
    parser.add_argument("--web", action="store_true", help="启动 FastAPI Web 服务")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=8000, help="服务端口")
    args = parser.parse_args()

    if args.web:
        import uvicorn
        print("=" * 60)
        print(f" 启动 Bennett Web 服务: http://{args.host}:{args.port}")
        print("=" * 60)
        uvicorn.run("app.server:app", host=args.host, port=args.port, reload=False)
    else:
        agent = create_agent()
        asyncio.run(run_interactive_loop(agent))



if __name__ == "__main__":
    main()
