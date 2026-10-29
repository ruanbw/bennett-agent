"""Bennett Agent 入口：起 API 与后台循环；agent 装配在 `app.services.agent_factory`，别在这里再写一份。

API 与沙箱回收共用同一个事件循环（`_run_all`）：engine、限流器、Redis 客户端都是进程级单例。
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
    # 默认只监听回环：注册入口是开放的，绑 0.0.0.0 等于把它和上游模型额度开放给同网段。
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="监听地址（默认仅本机；需局域网访问请显式指定 0.0.0.0）",
    )
    parser.add_argument("--port", type=int, default=8000, help="服务端口（1-65535）")
    # 开发期热重启，production 下拒绝；实现是重启整个 main.py 子进程，见 `app/dev_reload.py`。
    parser.add_argument(
        "--reload",
        action="store_true",
        help="开发期热重启：保存 Python / 提示词 / .env 后自动重启（仅限非 production）",
    )
    return parser


def main() -> None:
    # 日志配置在 `app/config/logging_setup`：uvicorn 自己装 handler，这里再 basicConfig 会变成两套格式。
    configure_logging()
    # 要早于任何 HTTP 客户端构造：坏 no_proxy 会让 trust_env 的客户端直接失败（见 app/config/proxy_env.py）。
    sanitize_no_proxy_env()
    parser = build_arg_parser()
    args = parser.parse_args()

    # 端口越界时 uvicorn 在绑定阶段抛 OSError，不如在参数层直接拒绝。
    if not 1 <= args.port <= 65535:
        parser.error(f"--port 必须在 1..65535 之间，收到 {args.port}")

    if args.reload:
        # 生产环境直接拒绝：热重启会在文件变化时反复重启，每次都掐断在飞请求（预算 600s，优雅关闭只等 5s）。
        if settings.app_env == "production":
            parser.error(
                "--reload 是开发期功能，禁止在 APP_ENV=production 下使用；"
                "生产环境改配置请走重启/重建镜像，并接受在飞请求会被中断。"
            )
        return _run_dev_reload(host=args.host, port=args.port)

    return asyncio.run(_run_all(host=args.host, port=args.port))


async def _run_all(host: str, port: int) -> None:
    """一条命令跑全套：API + 沙箱回收。

    必须共用一个事件循环：engine、限流器、Redis 都是进程级单例，连接绑在创建它的 loop 上，起子进程就是两份连接池。
    停机顺序不能反：`serve()` 返回后才置 `stop`，否则正在销毁的沙箱会停在 `destroying`，没有任何代码能回收它。
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
            # 传 None：uvicorn 的 dictConfig 会换掉 `uvicorn*` 的 handler，覆盖我们配好的格式与访问日志开关。
            log_config=None,
            # 关掉：默认 proxy_headers 让本机连接凭 X-Forwarded-For 改写 client.host，绕开应用层的限流键信任配置。
            proxy_headers=False,
            # 不设宽限：在飞 SSE 最长 600s（`TOTAL_STREAM_BUDGET_SECONDS`），等所有任务结束会让 Ctrl-C 像卡死。
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
    """开发期热重启的入口：把 `--host/--port` 原样交给 `app/dev_reload.py`。

    父进程也配一次日志，好让「检测到变更」「子进程没起来」这类 supervisor 日志与子进程同格式。
    """
    from app.dev_reload import run_dev_reload

    # flush=True：stdout 重定向到文件时是块缓冲，不刷新横幅会压到退出才出现，并排在子进程日志之后。
    print("=" * 60, flush=True)
    print(f" Bennett Agent 开发热重启: http://{host}:{port}{docs_hint()}", flush=True)
    print(f" {frontend_hint()}", flush=True)
    print("=" * 60, flush=True)
    run_dev_reload(host=host, port=port)
    return 0


def frontend_hint() -> str:
    """启动横幅里的前端提示：本进程只提供 `/api/*`，不说清会让人以为打开 8000 就有界面。"""
    return "前端请另开：cd frontend && npm run dev（http://127.0.0.1:5173）"


def docs_hint() -> str:
    """文档入口提示；生产环境三件套已被关闭，不能无条件写“/docs”。"""
    from app.server import _api_docs_enabled

    return "（文档 /docs）" if _api_docs_enabled() else "（生产环境已关闭 API 文档）"


if __name__ == "__main__":
    main()
