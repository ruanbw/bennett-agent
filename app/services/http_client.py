"""共享的 httpx 客户端。

O-2：改动前每个调用点各自 `async with httpx.AsyncClient(...)`（`model_service` 每次
拉模型列表、`tavily_search` 每次搜索），因此没有连接池、没有 keep-alive，
每次请求都要重新建连。

超时按调用点传入（`client.get(..., timeout=3.0)`），因为两类上游的合理等待时间
差一个数量级：模型列表 3 秒就该放弃，网页搜索 20 秒仍属正常。

## 连接池为什么要显式限量

httpx 默认 `max_connections=100`。本进程里用这个客户端的只有三类调用（模型列表、
提供商同步、Tavily 搜索），没有哪一类需要上百条并发连接；而上游挂掉时，未限量的
连接池会把「同时打向一个已经不可用的地址」的句柄一直攒着，直到超时才回收。
显式限量让最坏情况有上界，也让 `close()` 需要收拾的东西是确定的。

## 换事件循环时旧客户端会被关掉

httpx 的内部原语绑定创建它的 loop，所以换 loop 必须重建客户端。此前旧客户端只是被
丢掉，没有任何人关它——里面的连接与 socket 直到进程退出才回收（测试里每个用例一个新
loop，这个泄漏尤其明显）。现在重建前先把旧的 aclose 掉；旧 loop 已经关闭、关不掉时
只记警告并把引用丢掉，不让异常冒出来。
"""

import asyncio
import logging

import httpx

from app.config.proxy_env import sanitize_no_proxy_env

logger = logging.getLogger(__name__)

#: 兜底超时。调用点应显式传更贴合的值。
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)

#: 连接池上限：本进程只有三类调用点，不需要上百条并发连接。
#: `max_keepalive_connections` 小于等于上限，空闲连接超时后自动回收。
DEFAULT_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10, keepalive_expiry=30.0)

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


def _discard_client(client: httpx.AsyncClient, owner_loop: asyncio.AbstractEventLoop | None, *, reason: str) -> None:
    """丢掉一个不能再用的客户端；**只在安全时**顺手关掉它。

    关不掉的两种情况都必须只记警告、不能抛：

    - 旧客户端属于另一个已经关闭的 loop（测试里每个用例一个 `asyncio.run`，生产里
      是「先建后换主循环」）：此时没有任何 loop 能 await 它的 `aclose()`，连接只能
      等进程退出回收。在**当前** loop 上给它排关闭任务反而更糟——httpx 内部的传输层
      绑定旧 loop，任务里会抛「attached to a different loop」并变成无人回收的异常。
    - 没有 running loop（同步上下文）：同样排不了任务。

    属于**当前** loop 的旧客户端（被手动 close 过、或 is_closed 之外的失效）则直接排
    关闭任务，资源立刻回到系统。
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
    """返回进程级共享客户端（惰性创建）。

    当前事件循环与创建时不同则重建：旧客户端已经绑死在另一个 loop 上，复用它会在
    第一次请求时 RuntimeError，比多建一个连接池更糟。重建时旧客户端会被关掉
    （见 `_discard_client`），不再只是丢引用。
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
        # 构造之前清理 no_proxy：坏值会让 httpx 的代理白名单非法，客户端一建就抛
        # InvalidURL（见 app/config/proxy_env.py）。放在这里而不是只在启动时清理，
        # 是因为库的调用方（测试、脚本）未必经过 main()。
        sanitize_no_proxy_env()
        _client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, limits=DEFAULT_LIMITS)
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
