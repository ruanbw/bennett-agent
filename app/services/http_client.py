"""共享的 httpx 客户端。

O-2：改动前每个调用点各自 `async with httpx.AsyncClient(...)`（`model_service` 每次
拉模型列表、`tavily_search` 每次搜索），因此没有连接池、没有 keep-alive，
每次请求都要重新建连。

超时按调用点传入（`client.get(..., timeout=3.0)`），因为两类上游的合理等待时间
差一个数量级：模型列表 3 秒就该放弃，网页搜索 20 秒仍属正常。
"""

import logging

import httpx

logger = logging.getLogger(__name__)

#: 兜底超时。调用点应显式传更贴合的值。
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)

_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """返回进程级共享客户端（惰性创建）。"""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
    return _client


async def close_http_client() -> None:
    """关闭共享客户端。由 FastAPI lifespan 在进程退出时调用。"""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
