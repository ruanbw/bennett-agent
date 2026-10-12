"""HTTP 客户端生命周期与管理测试。"""

import asyncio
import logging

import pytest

from app.services import http_client
from app.services.http_client import close_http_client, get_http_client


@pytest.mark.asyncio
async def test_get_http_client_recreates_if_none_or_closed():
    # 确保重置
    await close_http_client()
    assert http_client._client is None

    c1 = get_http_client()
    assert c1 is not None
    assert not c1.is_closed

    c2 = get_http_client()
    assert c1 is c2

    # 主动关闭客户端，再次调用应重新创建未关闭的新实例
    await c1.aclose()
    assert c1.is_closed

    c3 = get_http_client()
    assert c3 is not None
    assert not c3.is_closed
    assert c3 is not c1

    await close_http_client()
    assert http_client._client is None


@pytest.mark.asyncio
async def test_close_http_client_swallows_aclose_errors(monkeypatch, caplog):
    """关闭失败只记日志不上抛：调用点紧接着还要 engine.dispose()。"""
    c = get_http_client()

    async def broken_aclose():
        raise RuntimeError("network failure during shutdown")

    monkeypatch.setattr(c, "aclose", broken_aclose)

    with caplog.at_level(logging.WARNING, logger="app.services.http_client"):
        await close_http_client()

    # 即使 aclose 异常，_client 依然必须被置为 None，且日志里要留下痕迹
    assert http_client._client is None
    assert http_client._client_loop is None
    assert "关闭共享 HTTP 客户端失败" in caplog.text
    assert get_http_client() is not c


@pytest.mark.asyncio
async def test_client_records_the_loop_that_created_it():
    await close_http_client()

    client = get_http_client()

    assert http_client._client_loop is asyncio.get_running_loop()
    assert client is http_client._client

    await close_http_client()


def test_client_is_rebuilt_when_the_event_loop_changes():
    """httpx 内部原语绑定创建它的 loop，跨 loop 复用会 RuntimeError。"""

    async def create_client():
        return get_http_client()

    first = asyncio.run(create_client())
    second = asyncio.run(create_client())

    try:
        assert second is not first
        assert not second.is_closed
    finally:
        # 第一个客户端只是被丢弃（没有任何连接），顺手关掉避免资源警告
        asyncio.run(first.aclose())
        asyncio.run(close_http_client())

    assert http_client._client is None
    assert http_client._client_loop is None
