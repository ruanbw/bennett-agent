"""出站目标地址检查（API Key 不能送到元数据服务／任意主机）。

`model_service._reject_private_target` 是纯判定函数：元数据族地址任何配置下都拒绝，
环回/私网默认放行（本机 LiteLLM 网关是文档里的主要用法），严格模式才拦内网；公网
域名解析失败不当作内网，交给后续的连接错误。
"""

import socket

import pytest

from app.config import settings
from app.services import model_service


@pytest.mark.parametrize(
    "base_url",
    [
        # 元数据族：任何配置下都拒绝（169.254.169.254 是云厂商实例元数据服务）
        "http://169.254.169.254/latest/meta-data",
        "http://0.0.0.0:9999/v1",
        "http://[fe80::1]:9999/v1",
    ],
)
async def test_metadata_targets_are_always_rejected(base_url):
    with pytest.raises(model_service.ProviderFetchError) as excinfo:
        await model_service._reject_private_target(base_url)
    assert "链路本地" in str(excinfo.value)


@pytest.mark.parametrize(
    "base_url",
    [
        # 文档里最主要的用法：本机 LiteLLM 网关 / 内网自建 vLLM
        "http://127.0.0.1:3425/v1",
        "http://localhost:4000/v1",
        "http://10.0.0.1:8000/v1",
        "http://172.16.5.4/v1",
        "http://192.168.1.10/v1",
        "http://[::1]:9999/v1",
    ],
)
async def test_loopback_and_private_targets_are_allowed_by_default(base_url):
    await model_service._reject_private_target(base_url)


@pytest.mark.parametrize(
    "base_url",
    ["http://127.0.0.1:3425/v1", "http://localhost:4000/v1", "http://10.0.0.1:8000/v1"],
)
async def test_private_targets_are_rejected_in_strict_mode(monkeypatch, base_url):
    monkeypatch.setattr(settings, "provider_block_private_targets", True)
    with pytest.raises(model_service.ProviderFetchError) as excinfo:
        await model_service._reject_private_target(base_url)
    assert "内网" in str(excinfo.value)


def _public_getaddrinfo(host, port):  # noqa: ANN001, ARG001
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


@pytest.mark.parametrize(
    "base_url",
    ["https://api.deepseek.com/v1", "http://8.8.8.8:9999/v1", "http://93.184.216.34/v1"],
)
async def test_public_targets_are_allowed(monkeypatch, base_url):
    # 公网域名不该真的去解析：用例只关心这条规则是否放行
    monkeypatch.setattr(model_service.socket, "getaddrinfo", _public_getaddrinfo)
    await model_service._reject_private_target(base_url)


async def test_dns_failure_is_not_treated_as_an_internal_target(monkeypatch):
    def _unresolvable(host, port):  # noqa: ANN001, ARG001
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(model_service.socket, "getaddrinfo", _unresolvable)
    # 不抛：解析失败交给后面的连接错误，而不是在这里报「内网」
    await model_service._reject_private_target("https://provider.invalid/v1")
