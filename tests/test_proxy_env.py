"""no_proxy 清洗的回归测试。

背景见 `app/config/proxy_env.py`：方括号 IPv6（`[::1]`）会让 httpx 把代理白名单
拼成非法 URL，导致所有上游请求在发出之前就抛 `InvalidURL: Invalid port: ':1]'`。
"""

import logging
import os

import httpx
import pytest

from app.config.proxy_env import normalize_no_proxy, sanitize_no_proxy_env, unsupported_entries
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


# ---------------------------------------------------------------------
# 带端口的方括号 IPv6，以及无法解析的条目
# ---------------------------------------------------------------------
def test_normalize_unwraps_bracketed_ipv6_with_port():
    """`[::1]:8000` 同样会炸（拼成 `all://*[::1]:8000`）：归一化时丢掉端口。

    `no_proxy` 是按主机绕过代理，端口不参与匹配；裸 `::1:8000` 会被解析成**另一个**
    IPv6 地址，所以不能只去掉方括号。
    """
    assert normalize_no_proxy("127.0.0.1,[::1]:8000") == "127.0.0.1,::1"
    assert normalize_no_proxy("[::1]:8000") == "::1"


def test_unparseable_bracket_entries_are_dropped():
    """`[a:b]` 进 httpx 会让**整个**客户端构造失败；丢掉它的代价只是该主机仍走代理。"""
    assert normalize_no_proxy("[a:b]") == ""
    assert normalize_no_proxy("127.0.0.1,[a:b],localhost") == "127.0.0.1,localhost"


def test_bracket_entries_without_colon_are_kept():
    """不含冒号的方括号条目 httpx 能正常解析，不属于本问题。"""
    assert normalize_no_proxy("[not-an-ip]") == "[not-an-ip]"


def test_sanitize_reports_the_rewrite(caplog):
    env = {"no_proxy": "127.0.0.1,[::1]:8000"}

    with caplog.at_level(logging.WARNING, logger="app.config.proxy_env"):
        rewrites = sanitize_no_proxy_env(env)

    assert env["no_proxy"] == "127.0.0.1,::1"
    assert rewrites == ["no_proxy=127.0.0.1,[::1]:8000 -> 127.0.0.1,::1"]
    assert "丢弃" in caplog.text


@pytest.mark.asyncio
async def test_shared_client_builds_with_ipv6_and_port(monkeypatch):
    """回归：`no_proxy` 里带端口的方括号 IPv6 曾让客户端构造直接抛 InvalidURL。"""
    monkeypatch.setenv("no_proxy", "127.0.0.1,[::1]:8000")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,[::1]:8000")

    await close_http_client()
    client = get_http_client()
    try:
        assert not client.is_closed
        assert os.environ["no_proxy"] == "127.0.0.1,::1"
    finally:
        await close_http_client()


# ---------------------------------------------------------------------
# 网段写法：httpx 不认，必须告警（否则「内网直连」只是个错觉）
# ---------------------------------------------------------------------
def test_cidr_entries_are_detected():
    assert unsupported_entries("192.168.0.0/16") == ["192.168.0.0/16"]
    assert unsupported_entries("10.0.0.1, fd00::/8") == ["fd00::/8"]
    # 前缀长度越界、主机不是 IP 的写法不算网段条目录别
    assert unsupported_entries("10.0.0.0/33") == []
    assert unsupported_entries("example.com/8") == []
    assert unsupported_entries("localhost,::1") == []


def test_cidr_entry_is_warned_once_and_left_untouched(caplog):
    """值不改（改了也不会更好），但要在日志里说清楚它不生效。"""
    environ = {"NO_PROXY": "192.168.0.0/16,10.0.0.1"}

    with caplog.at_level("WARNING", logger="app.config.proxy_env"):
        first = sanitize_no_proxy_env(environ)
        second = sanitize_no_proxy_env(environ)

    # 方括号/网段都不需要改写，返回值是「没动过」
    assert first == [] and second == []
    assert environ["NO_PROXY"] == "192.168.0.0/16,10.0.0.1"

    warnings = [record.getMessage() for record in caplog.records]
    assert len([message for message in warnings if "192.168.0.0/16" in message]) == 1
    assert any("网段" in message and "仍会走代理" in message for message in warnings)


def test_clean_no_proxy_does_not_warn_about_cidr(caplog):
    environ = {"no_proxy": "localhost,127.0.0.1,::1"}

    with caplog.at_level("WARNING", logger="app.config.proxy_env"):
        sanitize_no_proxy_env(environ)

    assert not [record for record in caplog.records if "网段" in record.getMessage()]
