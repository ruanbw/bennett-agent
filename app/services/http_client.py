"""共享的 httpx 客户端。

O-2：改动前每个调用点各自 `async with httpx.AsyncClient(...)`（`model_service` 每次
拉模型列表、`tavily_search` 每次搜索），因此没有连接池、没有 keep-alive，
每次请求都要重新建连。

超时按调用点传入（`client.get(..., timeout=3.0)`），因为两类上游的合理等待时间
差一个数量级：模型列表 3 秒就该放弃，网页搜索 20 秒仍属正常。
"""

import asyncio
import logging

import httpx

from app.config.proxy_env import sanitize_no_proxy_env

logger = logging.getLogger(__name__)

#: 兜底超时。调用点应显式传更贴合的值。
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)

_client: httpx.AsyncClient | None = None
#: 创建 `_client` 时的 running loop。httpx 的内部原语绑定 loop，跨 loop 复用会
#: RuntimeError（测试里每个用例一个 loop，应用里也可能是「先建后换主循环」）。
_client_loop: asyncio.AbstractEventLoop | None = None


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        # 同步上下文里拿不到 loop，此时只能按「无 loop」处理
        return None


def get_http_client() -> httpx.AsyncClient:
    """返回进程级共享客户端（惰性创建）。

    当前事件循环与创建时不同则重建：旧客户端已经绑死在另一个 loop 上，复用它会在
    第一次请求时 RuntimeError，比多建一个连接池更糟。
    """
    global _client, _client_loop

    loop = _running_loop()
    if _client is None or _client.is_closed or _client_loop is not loop:
        # 构造之前清理 no_proxy：坏值会让 httpx 的代理白名单非法，客户端一建就抛
        # InvalidURL（见 app/config/proxy_env.py）。放在这里而不是只在启动时清理，
        # 是因为库的调用方（测试、脚本）未必经过 main()。
        sanitize_no_proxy_env()
        _client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        _client_loop = loop
    return _client


async def close_http_client() -> None:
    """关闭共享客户端。由 FastAPI lifespan 在进程退出时调用。

    关闭失败只记警告、不上抛：`app/server.py` 的 lifespan 紧接着还要
    `engine.dispose()`，让这里的一个异常把数据库清理一起跳过不值得。
    """
    global _client, _client_loop

    client, _client, _client_loop = _client, None, None
    if client is None or client.is_closed:
        return

    try:
        await client.aclose()
    except Exception:  # noqa: BLE001 - 关闭是 best-effort，异常只进日志
        logger.warning("关闭共享 HTTP 客户端失败", exc_info=True)
