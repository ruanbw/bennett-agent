"""no_proxy 清洗的回归测试。

背景见 `app/config/proxy_env.py`：方括号 IPv6（`[::1]`）会让 httpx 把代理白名单
拼成非法 URL，导致所有上游请求在发出之前就抛 `InvalidURL: Invalid port: ':1]'`。
"""

import logging
import os

import httpx
import pytest

from app.config.proxy_env import normalize_no_proxy, sanitize_no_proxy_env
from app.services.agent_factory import build_provider
from app.services.http_client import close_http_client, get_http_client

#: `[::1]` 是方括号 IPv6；同一项 `::1` 是 httpx 能正确识别的裸写法。
_BAD_NO_PROXY = "127.0.0.1,localhost,::1,[::1]"
_GOOD_NO_PROXY = "127.0.0.1,localhost,::1"


def test_normalize_unwraps_bracketed_ipv6():
    assert normalize_no_proxy(_BAD_NO_PROXY) == _GOOD_NO_PROXY


def test_normalize_leaves_clean_value_untouched():
    value = "127.0.0.1,localhost,::1,*.example.com"
    assert normalize_no_proxy(value) == value


def test_normalize_leaves_non_ipv6_brackets_untouched():
    # 方括号里不是合法 IPv6：不属于本问题，原样保留
    assert normalize_no_proxy("[not-an-ip]") == "[not-an-ip]"


def test_sanitize_rewrites_both_env_names_and_reports(caplog):
    env = {"no_proxy": _BAD_NO_PROXY, "NO_PROXY": _BAD_NO_PROXY}

    with caplog.at_level(logging.WARNING, logger="app.config.proxy_env"):
        rewrites = sanitize_no_proxy_env(env)

    assert env["no_proxy"] == _GOOD_NO_PROXY
    assert env["NO_PROXY"] == _GOOD_NO_PROXY
    assert len(rewrites) == 2
    assert "no_proxy" in caplog.text


def test_sanitize_is_idempotent():
    env = {"no_proxy": _GOOD_NO_PROXY}

    assert sanitize_no_proxy_env(env) == []
    assert env["no_proxy"] == _GOOD_NO_PROXY


@pytest.mark.asyncio
async def test_shared_client_builds_with_bracketed_ipv6(monkeypatch):
    """回归：坏 no_proxy 曾让 httpx 客户端**构造**就抛 InvalidURL，请求根本发不出去。"""
    monkeypatch.setenv("no_proxy", _BAD_NO_PROXY)
    monkeypatch.setenv("NO_PROXY", _BAD_NO_PROXY)

    await close_http_client()
    client = get_http_client()
    try:
        assert not client.is_closed
        assert os.environ["no_proxy"] == _GOOD_NO_PROXY
        assert os.environ["NO_PROXY"] == _GOOD_NO_PROXY
        # 代理白名单里不能再出现 `all://*[::1]` 这种 httpx 解析不了的挂载点
        mounts = httpx._utils.get_environment_proxies()
        assert not any(key.endswith("[::1]") and "://*" in key for key in mounts)
    finally:
        await close_http_client()


def test_build_provider_builds_with_bracketed_ipv6(monkeypatch):
    """回归：聊天上游的客户端同样在构造阶段受坏 no_proxy 影响。"""
    monkeypatch.setenv("no_proxy", _BAD_NO_PROXY)
    monkeypatch.setenv("NO_PROXY", _BAD_NO_PROXY)

    provider = build_provider()  # 修复前抛 httpx.InvalidURL

    assert provider is not None
