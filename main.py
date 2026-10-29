"""Bennett Agent 入口：起 API 与后台循环。

Agent 装配不在本文件——它属于 `app.services.agent_factory`。此前这里有一份
`create_agent()`，与 `chat_service` 里的版本几乎相同但少传 `reasoning_effort`
与 `model_settings`，导致同一份配置跑出不同的 agent（R-3）。

一条命令跑全套：API + 沙箱回收共用同一个事件循环，见 `_run_all`。
"""

import argparse
import asyncio
import logging

from app.config import settings
from app.config.logging_setup import configure_logging
from app.config.proxy_env import sanitize_no_proxy_env

logger = logging.getLogger(__name__)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bennett Agent 运行入口")
    # 默认只监听回环：/api/chat 已要求登录，但注册入口是开放的；绑到 0.0.0.0
    # 等于把注册入口与上游模型额度开放给同网段
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="监听地址（默认仅本机；需局域网访问请显式指定 0.0.0.0）",
    )
    parser.add_argument("--port", type=int, default=8000, help="服务端口（1-65535）")
    # 开发期热重启。它**禁止在 production 下使用**（理由见 main() 里的拒绝逻辑）；
    # 实现不是 uvicorn 的 --reload，而是「重启完整的 main.py 子进程」，
    # 见 `app/dev_reload.py` 的模块 docstring——那边记录了为什么不能直接借 uvicorn 的。
    parser.add_argument(
        "--reload",
        action="store_true",
        help="开发期热重启：保存 Python / 提示词 / .env 后自动重启（仅限非 production）",
    )
    return parser


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

    return asyncio.run(_run_all(host=args.host, port=args.port))


async def _run_all(host: str, port: int) -> None:
    """一条命令跑全套：API + 沙箱回收。

    ## 为什么是 asyncio 而不是启动子进程

    两个循环必须共用**同一个事件循环**：`app/db.py` 的 engine、限流器、Redis 客户端都是
    进程级单例，而它们内部的 asyncpg / httpx 连接绑在创建它的 loop 上。起子进程就变成
    「两份连接池 + 两份限流计数」，正是 `docker-compose` 里多容器部署时要靠 Redis
    共享状态来解决的问题——本地没必要背这份复杂度。

    ## 停机的顺序

    uvicorn 收到 SIGTERM/SIGINT 后会先停止接收新请求、等新请求收尾，`serve()` 才返回。
    这时才置 `stop`：回收循环把手上这一台处理完（见 `sandbox/reaper.py` 的
    `wait_for(stop.wait(), ...)`），最后统一释放连接池。顺序反了的话，正在销毁的沙箱会
    停在 `destroying`——而那个状态没有任何代码能回收它。
    """
    import uvicorn

    from app.db import engine, verify_schema_at_head
    from app.services.http_client import close_http_client
    from app.services.redis_client import close_redis
    from app.services.sandbox.reaper import run_reaper

    await verify_schema_at_head()

    stop = asyncio.Event()
    loops = [asyncio.create_task(run_reaper(stop), name="sandbox-reaper")]
    server = uvicorn.Server(
        uvicorn.Config(
            "app.server:app",
            host=host,
            port=port,
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
    )

    print("=" * 60)
    print(f" Bennett Agent API: http://{host}:{port}{docs_hint()}")
    print(f" {frontend_hint()}")
    print("=" * 60)

    try:
        await server.serve()
    finally:
        stop.set()
        # 给它们一点时间把手上的活收尾；超时也不阻塞退出（SIGKILL 兜底）。
        await asyncio.wait(loops, timeout=10)
        for task in loops:
            task.cancel()
        try:
            await close_redis()
        except Exception:  # noqa: BLE001 - 收尾失败不遮蔽其他清理
            logger.warning("关闭 Redis 连接失败", exc_info=True)
        try:
            await close_http_client()
        except Exception:  # noqa: BLE001
            logger.warning("关闭共享 HTTP 客户端失败", exc_info=True)
        try:
            await engine.dispose()
        except Exception:  # noqa: BLE001
            logger.warning("释放数据库连接失败", exc_info=True)


def _run_dev_reload(host: str, port: int) -> int:
    """开发期热重启的入口：把 `--host/--port` 原样交给 supervisor。

    这里**不做** `configure_logging()` 之外的准备工作：真正干活的是
    `app/dev_reload.py`，它负责监听文件、起停 `main.py` 子进程。父进程只打印一行
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
