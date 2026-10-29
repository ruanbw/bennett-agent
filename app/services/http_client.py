"""共享的 httpx 客户端：全进程一个连接池，超时按调用点传入。

连接池显式限量（20）：上游挂掉时未限量的池会把打向已不可用地址的句柄攒到超时才回收。
httpx 原语绑定创建它的 loop，换 loop 必须重建，旧客户端先 aclose（见 `_discard_client`）。
"""

import asyncio
import logging

import httpx

from app.config.proxy_env import sanitize_no_proxy_env

logger = logging.getLogger(__name__)

#: 兜底超时。调用点应显式传更贴合的值。
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)

#: 连接池上限：本进程只有三类调用点，不需要上百条并发连接；空闲连接超时后回收。
DEFAULT_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=30.0)

_client: httpx.AsyncClient | None = None
#: 创建 `_client` 时的 loop：httpx 原语绑定 loop，跨 loop 复用会 RuntimeError（测试每例一个 loop）。
_client_loop: asyncio.AbstractEventLoop | None = None


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        # 同步上下文里拿不到 loop，此时只能按「无 loop」处理
        return None


def _discard_client(client: httpx.AsyncClient, owner_loop: asyncio.AbstractEventLoop | None, *, reason: str) -> None:
    """丢掉一个不能再用的客户端，并且只在安全时顺手关掉它。

    旧客户端属于另一个 loop 时排关闭任务反而更糟：传输层绑旧 loop，任务会抛「attached to a different loop」。
    没有 running loop 时同理，两种都只记警告。
    """
    if client.is_closed:
        return

    current = _running_loop()
    if owner_loop is not None and owner_loop is not current:
        logger.warning(
            "共享 HTTP 客户端所属的事件循环已失效（%s），其连接将由进程退出回收",
            reason,
        )
        return

    if current is not None:
        current.create_task(client.aclose())
        return

    logger.warning("共享 HTTP 客户端已失效但当前没有事件循环（%s），连接将由进程退出回收", reason)


def get_http_client() -> httpx.AsyncClient:
    """返回进程级共享客户端（惰性创建）；当前 loop 与创建时不同则重建。

    复用绑在旧 loop 上的客户端会在第一次请求时 RuntimeError，比多建一个连接池更糟。
    """
    global _client, _client_loop

    loop = _running_loop()
    if _client is None or _client.is_closed or _client_loop is not loop:
        previous, previous_loop = _client, _client_loop
        if previous is not None:
            _discard_client(
                previous,
                previous_loop,
                reason="事件循环已切换" if previous_loop is not loop else "客户端已关闭",
            )
        # 构造前清理 no_proxy：坏值会让客户端一建就抛 InvalidURL（见 app/config/proxy_env.py）；
        # 放在这里而不是只在启动时清，是因为测试与脚本未必经过 main()。
        sanitize_no_proxy_env()
        _client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, limits=DEFAULT_LIMITS)
        _client_loop = loop
    return _client


async def close_http_client() -> None:
    """关闭共享客户端，由 lifespan 在进程退出时调用。

    关闭失败只记警告：lifespan 紧接着还要 `engine.dispose()`，不该因它跳过数据库清理。
    """
    global _client, _client_loop

    client, _client, _client_loop = _client, None, None
    if client is None or client.is_closed:
        return

    try:
        await client.aclose()
    except Exception:  # noqa: BLE001 - 关闭是 best-effort，异常只进日志
        logger.warning("关闭共享 HTTP 客户端失败", exc_info=True)
