"""出站地址守卫：提供商地址必须是能安全送出明文 API Key 的地方。
元数据族永远拒绝，环回与私网只在 `settings.provider_block_private_targets` 开启时才拒绝。
"""

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlsplit

from app.config import settings
from app.services.model_service.errors import ProviderFetchError

logger = logging.getLogger(__name__)


#: 云厂商元数据地址。它们不都是链路本地：只靠 `is_link_local` 会漏掉 AWS IPv6 IMDS（ULA）
#: 与阿里云的 100.100.100.200（CGNAT），而默认模式下私网又是放行的。
CLOUD_METADATA_ADDRESSES: frozenset[ipaddress.IPv4Address | ipaddress.IPv6Address] = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)

#: NAT64 前缀（RFC 6052 / RFC 8215）：最后 32 位是内嵌的 IPv4。
_NAT64_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)


def _embedded_ipv4(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | None:
    """取出 IPv6 地址里内嵌的 IPv4（若存在）：只判外层 IPv6 会得出错误结论。
    `64:ff9b::a9fe:a9fe` 的 `is_global` 是 True，放行它等于允许访问 169.254.169.254。
    """
    if not isinstance(address, ipaddress.IPv6Address):
        return None

    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address.teredo is not None:
        # teredo 返回 (server, client)，服务端地址是实际可达的那一跳
        return address.teredo[0]

    packed = int(address)
    for network in _NAT64_NETWORKS:
        if address in network:
            return ipaddress.IPv4Address(packed & 0xFFFFFFFF)
    # IPv4-compatible ::a.b.c.d（:: 与 ::1 除外）同样按内嵌 IPv4 处理。
    if (packed >> 32) == 0 and packed not in (0, 1):
        return ipaddress.IPv4Address(packed & 0xFFFFFFFF)
    return None


def _classify_addresses(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """外层地址 + 内嵌 IPv4（若有）一起参与判定。"""
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [address]
    embedded = _embedded_ipv4(address)
    if embedded is not None:
        addresses.append(embedded)
    return addresses


def _is_metadata_range(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """元数据族：任何配置下都不接受的出站目标（链路本地 / 未指定 / 组播 / 云元数据地址）。
    内嵌 IPv4 也要拆开判，它是盲 SSRF 里唯一不需要先拿到凭据的攻击面。
    """
    for candidate in _classify_addresses(address):
        if candidate in CLOUD_METADATA_ADDRESSES:
            return True
        if candidate.is_link_local or candidate.is_unspecified or candidate.is_multicast:
            return True
    return False


def _is_internal_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """环回 / 私有 / 保留段：只在严格模式下拒绝。用 `is_global` 取反，比逐类判断更难漏。
    默认不启用：本机 LiteLLM 网关与自建 vLLM 常在内网，一刀切会拦掉文档里的主要用法。
    """
    embedded = _embedded_ipv4(address)
    if embedded is not None:
        return not embedded.is_global
    return not address.is_global


async def _resolve_addresses(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """把主机名解析成 IP 列表；字面量 IP 直接返回，解析失败返回空列表。
    不能只认字面量：十进制写法（`http://2130706433/`）在解析层等价于 127.0.0.1。
    """
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass

    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except OSError:
        return []

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        # 带 scope id 的 IPv6（fe80::1%en0）不是 ip_address 能直接吃的写法
        literal = str(info[4][0]).split("%", 1)[0]
        try:
            addresses.append(ipaddress.ip_address(literal))
        except ValueError:
            continue
    return addresses


async def reject_private_target(base_url: str) -> None:
    """出站前检查目标地址，命中则抛 `ProviderFetchError`。
    调用点必须覆盖全部出站路径（建/改提供商、拉模型、`_build_target` 的对话出站），少一处就能被绕过。
    """
    host = urlsplit(base_url).hostname
    if not host:
        # 缺主机名/畸形地址：让 httpx 报它自己的错。
        return

    for address in await _resolve_addresses(host):
        if _is_metadata_range(address):
            raise ProviderFetchError(f"Base URL 不允许指向链路本地或未指定地址（{address}）")
        if settings.provider_block_private_targets and _is_internal_address(address):
            raise ProviderFetchError(f"Base URL 不允许指向内网地址（{address}）")


async def _warn_plaintext_key_over_public_http(
    base_url: str,
    *,
    provider_name: str,
    has_api_key: bool,
) -> None:
    """`http://` 且主机解析为公网地址时，提示 API Key 会以明文经过公网。
    只告警不拒绝；只要有一个解析结果是内网就不下结论——管理员可能在 VPN 后面。
    """
    if not has_api_key:
        return

    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or not parsed.hostname:
        return

    addresses = await _resolve_addresses(parsed.hostname)
    if not addresses or any(_is_internal_address(address) for address in addresses):
        return

    logger.warning(
        "提供商 %s 的 Base URL 使用明文 http 且指向公网地址 %s，API Key 将以明文经过公网传输；建议改用 https",
        provider_name,
        parsed.hostname,
    )
