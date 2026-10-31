"""客户端 IP 解析与限流桶键：把「请求从哪来」还原成可计数的稳定键。

反代下 `client.host` 是代理地址，全站共享一个桶；直接采信 `X-Forwarded-For` 又等于把限流桶
的选择权交给客户端（自填标头即可绕过）。所以只有直连对端落在受信网段内才读标头，且从右往左
跳过受信代理、取第一个非代理地址。本模块不依赖 FastAPI：输入就是裸的 host 与标头字典，
`Request` 的拆包留给 api 层。
"""

from __future__ import annotations

import ipaddress
import logging

logger = logging.getLogger(__name__)

_warned_missing_proxy_ips = False


def _parse_ip(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """把 XFF / client.host 里的单个地址解析成 IP；解析失败返回 None。

    兼容 `[::1]`、`[::1]:443`、`1.2.3.4:5678` 这几种代理常见的写法。
    """
    if not value:
        return None
    candidate = value.strip()
    if candidate.startswith("["):
        closing = candidate.find("]")
        if closing == -1:
            return None
        candidate = candidate[1:closing]
    elif candidate.count(":") == 1 and "." in candidate:
        candidate = candidate.rsplit(":", 1)[0]
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def bucket_ip_address(value: str | None) -> str:
    """把来源地址归一化成限流桶键。

    IPv6 的一个订阅通常是一整个 /64，按完整地址计数就能轮换地址绕过限流（I-07），故归并到
    /64，IPv4-mapped 先解包；解析不了的值原样返回，不能归到 "unknown" 而放大误伤。
    """
    ip = _parse_ip(value)
    if ip is None:
        return value or "unknown"
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def parse_trusted_proxy_networks(raw: str | None) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """解析 `TRUSTED_PROXY_IPS`（逗号分隔的 IP/CIDR）。

    空值返回空列表——空列表意味着不采信任何代理标头（fail closed）。非法条目跳过并告警，
    不静默把整份配置作废。
    """
    networks = []
    for item in (raw or "").split(","):
        cleaned = item.strip()
        if not cleaned:
            continue
        try:
            networks.append(ipaddress.ip_network(cleaned, strict=False))
        except ValueError:
            logger.warning("TRUSTED_PROXY_IPS 里的条目不是合法 IP/CIDR，已忽略: %r", cleaned)
    return networks


def resolve_client_ip(
    direct_host: str | None,
    headers: dict[str, str] | None = None,
    trusted_headers: list[str] | None = None,
    trusted_proxies: str | None = None,
) -> str:
    """从受信任的标头或直连客户端解析真实客户端 IP。

    直连对端必须落在 `TRUSTED_PROXY_IPS` 里，否则完全不采信标头（L-10：能被直连的后端任人自填
    `X-Forwarded-For` 换桶）。XFF 从右往左跳过受信代理，第一个非代理地址才是真实客户端 IP。
    """
    global _warned_missing_proxy_ips

    if not trusted_headers or not headers:
        return bucket_ip_address(direct_host)

    networks = parse_trusted_proxy_networks(trusted_proxies)
    if not networks:
        if not _warned_missing_proxy_ips:
            _warned_missing_proxy_ips = True
            logger.warning(
                "TRUSTED_PROXY_HEADERS 已配置但 TRUSTED_PROXY_IPS 为空：为避免直连伪造标头，"
                "代理标头不会被采信，限流按 client.host 计数。请在 TRUSTED_PROXY_IPS 里配置代理网段。"
            )
        return bucket_ip_address(direct_host)

    direct_ip = _parse_ip(direct_host)
    if direct_ip is None or not any(direct_ip in network for network in networks):
        # 直连对端不是受信代理：它带来的任何标头都不可信
        return bucket_ip_address(direct_host)

    lower_headers = {key.lower(): value for key, value in headers.items()}
    chain: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for header_name in trusted_headers:
        raw_val = lower_headers.get(header_name.lower().strip())
        if not raw_val or not raw_val.strip():
            continue
        chain = [parsed for item in raw_val.split(",") if (parsed := _parse_ip(item)) is not None]
        break

    if not chain:
        return bucket_ip_address(direct_host)

    for candidate in reversed(chain):
        if not any(candidate in network for network in networks):
            return bucket_ip_address(str(candidate))
    # 整条链都是受信代理（异常配置）：退回最左项，至少不会退化成空串
    return bucket_ip_address(str(chain[0]))


def client_key(host: str | None, scope: str = "") -> str:
    """限流键：`[scope:]来源 IP`；缺失时归入同一个桶，宁可保守也不要放过。

    `scope` 把同一 IP 的不同端点分到不同的桶：共用一桶时，有人在一个 NAT 出口猛试密码打满
    配额，同一出口的正常注册也做不了，而两者要挡的攻击面不同。
    """
    return f"{scope}:{host or 'unknown'}" if scope else (host or "unknown")
