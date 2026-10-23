"""Bennett Agent 入口：CLI 交互模式与 FastAPI Web 模式。

Agent 装配不在本文件——它属于 `app.services.agent_factory`。此前这里有一份
`create_agent()`，与 `chat_service` 里的版本几乎相同但少传 `reasoning_effort`
与 `model_settings`，导致 CLI 与 Web 用同一份配置跑出不同的 agent（R-3）。

## CLI 与 Web 的差异是刻意的，但不能是**偶然**的

Web 那边有一整套生成守卫：历史长度上限、token 预算预检、总时长预算、工具异常隔离。
CLI 早期一样都没有，于是「本地跑得通、Web 却拒」这类差异会长得莫名其妙。现在 CLI
复用同一份预算计算（`token_budget`）与同一个装配点（`agent_factory`），只在
**展示方式**上保持区别：CLI 只能看文本，看不到工具调用与澄清弹窗。
"""

import argparse
import asyncio
import logging
import sys
from typing import Literal

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage

from app.config import settings
from app.config.logging_setup import configure_logging
from app.config.proxy_env import sanitize_no_proxy_env
from app.db import create_db_and_tables, engine
from app.services.agent_factory import build_agent, render_system_prompt
from app.services.http_client import close_http_client
from app.services.model_service import (
    ChatModelSnapshot,
    get_default_model_key,
    resolve_chat_model,
    seed_default_provider,
)
from app.services.token_budget import (
    TOOL_SCHEMA_OVERHEAD_TOKENS,
    build_budget,
    estimate_agent_messages_tokens,
    estimate_tokens,
)

logger = logging.getLogger(__name__)

CliCommand = Literal["exit", "clear", "help"]


def parse_cli_command(raw: str) -> CliCommand | None:
    """把一行输入识别成内置命令（大小写不敏感，前后空白忽略）。

    抽成纯函数是为了能单测：命令词与普通提问共用一行输入，误把 "clear" 当消息发给
    模型会浪费一次调用；反过来把普通提问误判成命令则会丢消息。
    """
    match raw.strip().lower():
        case "exit" | "quit" | "q":
            return "exit"
        case "clear":
            return "clear"
        case "help" | "?":
            return "help"
        case _:
            return None


def format_cli_model_label(env_model: str, resolved_key: str) -> str:
    """启动横幅里的模型文案。

    只打印 `MODEL_NAME` 会骗人：当环境变量不是后台合法路由键时，CLI 实际用的是数据库
    默认模型。两者不同时必须同时写出来，否则用户会按错误的名字排查。
    """
    requested = env_model.strip()
    resolved = resolved_key.strip()
    if not requested:
        return resolved or "未配置"
    if not resolved or resolved == requested:
        return resolved or requested
    return f"{requested}（实际路由：{resolved}）"


def _budget_for_cli(model_key: str, history: list[ModelMessage], context_window: int | None, max_output):
    """按与 Web 相同的规则算一次预算。

    与 `chat_service` 的差别只在「历史从哪里来」：那边是客户端全量回放的
    `ChatMessage`，这里是 `AgentRunResult.all_messages()` 的 pydantic-ai 对象。
    上限公式、预留量、安全系数全部走同一份 `build_budget`。
    """
    if context_window is None or context_window <= 0:
        return None
    used = estimate_agent_messages_tokens(history)
    overhead = estimate_tokens(render_system_prompt()) + TOOL_SCHEMA_OVERHEAD_TOKENS
    return build_budget(
        model_key,
        context_window,
        max_output,
        used_tokens=used,
        overhead_tokens=overhead,
        used_tokens_include_overhead=False,
    )


async def run_interactive_loop(
    agent: Agent,  # noqa: ANN401
    model_key: str = "",
    limits: tuple[int | None, int | None] = (None, None),
) -> None:
    """运行终端交互式 Agent Loop，并以流式输出展示。

    `limits` 是 `(context_window, max_output_tokens)`，由 `build_cli_agent` 在一次解析
    里顺带取到；以前这里在入口处单独 `get_model_limits(model_key)` 再全量扫一遍启用模型。
    """
    print("=" * 60)
    print(" Bennett Agent 启动成功 (流式响应模式)")
    print(f" [环境] APP_ENV:          {settings.app_env}")
    print(f" [模型] MODEL_NAME:       {format_cli_model_label(settings.model_name, model_key)}")
    print(f" [接口] LITELLM_API_BASE: {settings.litellm_api_base}")
    print("=" * 60)
    print("提示: 输入你的问题；exit / quit / q 退出，clear 重置上下文，help 查看命令\n")

    message_history: list[ModelMessage] = []
    context_window, max_output = limits

    while True:
        try:
            raw_input = await asyncio.to_thread(input, "User > ")
            user_input = raw_input.strip()
        except (KeyboardInterrupt, EOFError):
            print("\n退出 Agent。")
            break

        if not user_input:
            continue

        command = parse_cli_command(user_input)
        if command == "exit":
            print("再见！")
            break

        if command == "help":
            print("可用命令：exit / quit / q 退出；clear 重置上下文；help / ? 查看本帮助\n")
            continue

        if command == "clear":
            message_history.clear()
            print("[上下文已重置]\n")
            continue

        # 预算预检：Web 在每轮开头做同样的事。不做的话长会话必然撞上游 400，
        # 而用户看到的只有一句“模型不可用”。
        #
        # 估算放进线程：真分词器不存在，估算是一段纯 Python 循环，长中文历史要几百
        # 毫秒（见 `token_budget.estimate_tokens` 的性能说明），这期间事件循环里别的
        # 任务（HTTP、定时器）都会被挡住。
        budget = await asyncio.to_thread(_budget_for_cli, model_key, message_history, context_window, max_output)
        if budget is not None and budget.over_limit:
            print(
                f"\n[上下文已满] 本会话约 {budget.used_tokens:,} tokens，"
                f"超出可用上限 {budget.limit_tokens:,}（模型窗口 {budget.context_window:,}）。\n"
                "请输入 'clear' 重置上下文后继续。\n"
            )
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

        except KeyboardInterrupt:
            # Ctrl-C 中断一轮生成（而不是退出程序）：保留已输出的内容与历史
            print("\n[已中断本轮]\n")
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
    # 开发期热重启。它**隐含 --web** 且**禁止在 production 下使用**（理由见 main() 里的
    # 拒绝逻辑）；实现不是 uvicorn 的 --reload，而是「重启完整的 main.py --web 子进程」，
    # 见 `app/dev_reload.py` 的模块 docstring——那边记录了为什么不能直接借 uvicorn 的。
    parser.add_argument(
        "--reload",
        action="store_true",
        help="开发期热重启：保存 Python / 提示词 / .env 后自动重启（隐含 --web，仅限非 production）",
    )
    return parser


async def build_cli_agent() -> tuple[Agent, ChatModelSnapshot]:  # noqa: ANN401
    """CLI 的装配入口：与 Web 共用管理后台里的提供商配置。

    CLI 不走 FastAPI lifespan，所以自己建表 + 引导一次默认提供商。解析顺序：
    环境变量里的 `MODEL_NAME` 若是后台的合法路由键就直用它，否则取后台默认模型；
    数据库还没有任何配置时退回旧版环境变量路径（`target=None`）。

    两个候选键合并成**一次**解析：`resolve_chat_model` 同时给出 target 与窗口上限，
    调用方不必再为预算预检单独查一遍库（以前 `resolve_model_target` 被调了两次）。
    返回整个快照而不是 `(agent, key)`：快照里的窗口上限就是 CLI 预算预检要的数字。
    """
    await create_db_and_tables()
    await seed_default_provider()

    snapshot = await resolve_chat_model(settings.model_name)
    if snapshot.target is None:
        default_key = await get_default_model_key()
        if default_key and default_key != snapshot.key:
            snapshot = await resolve_chat_model(default_key)

    return build_agent(snapshot.key, target=snapshot.target), snapshot


async def _run_cli() -> None:
    """CLI 主流程：**单一事件循环**，并在退出时释放资源。

    为什么不能拆成两次 `asyncio.run`：Agent 来自 `agent_factory` 的 `lru_cache`，
    里面捧着 `AsyncOpenAI`→httpx 客户端。httpx 的内部原语绑在创建它们的 loop 上，
    跨 loop 复用会 `RuntimeError`（`app/services/http_client.py` 记录了同一个坑）。

    之前能跑只是因为 `build_cli_agent` 阶段恰好没有建连——一旦以后加个预热/探活调用，
    第一个 prompt 就会炸。

    退出时补上 `close_http_client` 与 `engine.dispose`：Web 那边由 lifespan 的
    finally 做，CLI 不走 lifespan，不补就会留下未折叠的 SQLite WAL。
    """
    try:
        agent, snapshot = await build_cli_agent()
        await run_interactive_loop(agent, snapshot.key, (snapshot.context_window, snapshot.max_output_tokens))
    finally:
        try:
            await close_http_client()
        except Exception:  # noqa: BLE001 - 单个资源失败不能遮蔽另一个
            logger.warning("关闭共享 HTTP 客户端失败", exc_info=True)
        try:
            await engine.dispose()
        except Exception:  # noqa: BLE001 - 同上
            logger.warning("释放数据库连接失败", exc_info=True)


def main() -> None:
    # 级别、格式与第三方噪音抑制都在 `app/config/logging_setup`：uvicorn 会自己装 handler
    # 且 propagate=False，只在这里 basicConfig 的话日志会出现两套格式。
    configure_logging()
    # 越早清理越好：坏 no_proxy 会让所有 trust_env 的 HTTP 客户端在构造阶段就失败
    # （见 app/config/proxy_env.py）。放在参数解析之前，配置有问题时先留下清晰日志。
    sanitize_no_proxy_env()
    parser = build_arg_parser()
    args = parser.parse_args()

    # 超范围的端口会让 uvicorn 在绑定阶段抛出难懂的 OSError，不如在参数层面直接拒绝。
    if not 1 <= args.port <= 65535:
        parser.error(f"--port 必须在 1..65535 之间，收到 {args.port}")

    if args.reload:
        # production 下直接拒绝而不是「警告后照跑」：热重启会让进程在文件变化时反复重启，
        # 而生产环境里任何一次重启都会掐断在飞请求（单轮 SSE 预算 600s，优雅关闭只等 5s）。
        # 更糟的是它让「进程为什么会重启」变成一个和部署动作无关的问题。
        if settings.app_env == "production":
            parser.error(
                "--reload 是开发期功能，禁止在 APP_ENV=production 下使用；"
                "生产环境改配置请走重启/重建镜像，并接受在飞请求会被中断。"
            )
        return _run_dev_reload(host=args.host, port=args.port)

    if args.web:
        import uvicorn

        print("=" * 60)
        print(f" Bennett Agent API: http://{args.host}:{args.port}{docs_hint()}")
        # 前端不在本进程里：只写端口号会让人以为打开 8000 就能看到界面。
        print(f" {frontend_hint()}")
        print("=" * 60)
        uvicorn.run(
            "app.server:app",
            host=args.host,
            port=args.port,
            reload=False,
            # 不要 uvicorn 的 dictConfig：它在启动时会把 `uvicorn*` 三个 logger 的 handler
            # 换成自己那套（`INFO:     ...`），`configure_logging()` 设置的格式与
            # 「关掉访问日志」都会被覆盖。传 None 表示「日志已经配好了，别动」。
            log_config=None,
            # uvicorn 默认 proxy_headers=True 且 forwarded_allow_ips=127.0.0.1：本机连接
            # （包括反向代理）可以凭 X-Forwarded-For 直接改写 request.client.host，
            # 于是「只信 app/api/rate_limit.py 里显式配置的 trusted_proxy_headers」的
            # 模型被绕过，客户端能自选限流键。关掉它，信任判断只留在应用层一处。
            proxy_headers=False,
            # 不设宽限：在飞的 SSE 最长 `TOTAL_STREAM_BUDGET_SECONDS`（600s），
            # 走默认的等所有任务结束会让 Ctrl-C 看起来卡死。
            timeout_graceful_shutdown=5,
        )
    else:
        asyncio.run(_run_cli())


def _run_dev_reload(host: str, port: int) -> int:
    """开发期热重启的入口：把 `--host/--port` 原样交给 supervisor。

    这里**不做** `configure_logging()` 之外的准备工作：真正干活的是
    `app/dev_reload.py`，它负责监听文件、起停 `main.py --web` 子进程。父进程只打印一行
    横幅，日志由子进程按 `configure_logging()` 的格式输出（父进程自己也配一次，
    是为了让「检测到变更」「子进程没起来」这类 supervisor 日志同样带统一格式）。
    """
    from app.dev_reload import run_dev_reload

    # flush=True：stdout 被重定向到文件时是块缓冲，不刷新的话这段横幅会一直压到进程
    # 退出才出现——看起来像「热重启没打横幅」，而且会排在所有子进程日志之后。
    print("=" * 60, flush=True)
    print(f" Bennett Agent 开发热重启: http://{host}:{port}{docs_hint()}", flush=True)
    print(f" {frontend_hint()}", flush=True)
    print("=" * 60, flush=True)
    run_dev_reload(host=host, port=port)
    return 0


def frontend_hint() -> str:
    """启动横幅里的前端提示。

    前后端分离：本进程只提供 `/api/*`。说清「另开 Vite」是必要的——只写端口号会让
    人以为打开 8000 就能看到界面，然后对着 JSON 404 找半天。
    """
    return "前端请另开：cd frontend && npm run dev（http://127.0.0.1:5173）"


def docs_hint() -> str:
    """文档入口提示；生产环境三件套已被关闭，不能无条件写“/docs”。"""
    from app.server import _api_docs_enabled

    return "（文档 /docs）" if _api_docs_enabled() else "（生产环境已关闭 API 文档）"


if __name__ == "__main__":
    main()
