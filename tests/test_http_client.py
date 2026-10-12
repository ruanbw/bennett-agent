"""HTTP 客户端生命周期与管理测试。"""

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
async def test_close_http_client_ensures_none_on_exception(monkeypatch):
    c = get_http_client()

    async def broken_aclose():
        raise RuntimeError("network failure during shutdown")

    monkeypatch.setattr(c, "aclose", broken_aclose)

    with pytest.raises(RuntimeError, match="network failure during shutdown"):
        await close_http_client()

    # 即使 aclose 异常，_client 依然必须被置为 None
    assert http_client._client is None
