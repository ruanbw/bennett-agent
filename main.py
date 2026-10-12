"""Bennett Agent 入口：CLI 交互模式与 FastAPI Web 模式。

Agent 装配不在本文件——它属于 `app.services.agent_factory`。此前这里有一份
`create_agent()`，与 `chat_service` 里的版本几乎相同但少传 `reasoning_effort`
与 `model_settings`，导致 CLI 与 Web 用同一份配置跑出不同的 agent（R-3）。
"""

import argparse
import asyncio
import logging
import sys

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage

from app.config import settings
from app.services.agent_factory import build_agent

logger = logging.getLogger(__name__)


async def run_interactive_loop(agent: Agent) -> None:  # noqa: ANN401
    """运行终端交互式 Agent Loop，并以流式输出展示。"""
    print("=" * 60)
    print(" Bennett Agent 启动成功 (流式响应模式)")
    print(f" [环境] APP_ENV:          {settings.app_env}")
    print(f" [模型] MODEL_NAME:       {settings.model_name}")
    print(f" [接口] LITELLM_API_BASE: {settings.litellm_api_base}")
    print("=" * 60)
    print("提示: 输入你的问题（输入 'exit' 或 'quit' 退出，输入 'clear' 重置上下文）\n")

    message_history: list[ModelMessage] = []

    while True:
        try:
            raw_input = await asyncio.to_thread(input, "User > ")
            user_input = raw_input.strip()
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
            async with agent.run_stream(user_input, message_history=message_history) as response:
                async for delta in response.stream_text(delta=True):
                    sys.stdout.write(delta)
                    sys.stdout.flush()

                # 将本轮消息追加到历史上下文
                message_history = response.all_messages()

            print("\n")

        except Exception:
            # 打到日志而不是只 print：CLI 里的失败同样需要 traceback 才能定位
            logger.exception("CLI 本轮执行失败")
            print("\n[执行异常] 详见上方日志\n")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bennett Agent 运行入口")
    parser.add_argument("--web", action="store_true", help="启动 FastAPI Web 服务")
    # 默认只监听回环：/api/chat 已要求登录，但注册入口是开放的；绑到 0.0.0.0
    # 等于把注册入口与上游模型额度开放给同网段
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="监听地址（默认仅本机；需局域网访问请显式指定 0.0.0.0）",
    )
    parser.add_argument("--port", type=int, default=8000, help="服务端口（1-65535）")
    return parser


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    parser = build_arg_parser()
    args = parser.parse_args()

    # 超范围的端口会让 uvicorn 在绑定阶段抛出难懂的 OSError，不如在参数层面直接拒绝。
    if not 1 <= args.port <= 65535:
        parser.error(f"--port 必须在 1..65535 之间，收到 {args.port}")

    if args.web:
        import uvicorn

        print("=" * 60)
        print(f" Bennett Agent API: http://{args.host}:{args.port}（文档 /docs）")
        print(" Web 界面由前端独立提供：cd frontend && npm run dev")
        print("=" * 60)
        uvicorn.run(
            "app.server:app",
            host=args.host,
            port=args.port,
            reload=False,
            # uvicorn 默认 proxy_headers=True 且 forwarded_allow_ips=127.0.0.1：本机连接
            # （包括反向代理）可以凭 X-Forwarded-For 直接改写 request.client.host，
            # 于是「只信 app/api/rate_limit.py 里显式配置的 trusted_proxy_headers」的
            # 模型被绕过，客户端能自选限流键。关掉它，信任判断只留在应用层一处。
            proxy_headers=False,
        )
    else:
        asyncio.run(run_interactive_loop(build_agent(settings.model_name)))


if __name__ == "__main__":
    main()
