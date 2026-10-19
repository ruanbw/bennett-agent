"""出站地址守卫：提供商地址必须是能安全送出明文 API Key 的地方。

两档强度：

- 元数据族（链路本地 / 未指定 / 组播）**永远拒绝**——盲 SSRF 打元数据服务是这条
  路径上唯一「不需要先拿到凭据」就能换到新凭据的攻击；
- 环回与私网**默认放行**（`settings.provider_block_private_targets = true` 才拒绝）。
  它是纵深防御而不是主防线：本机 LiteLLM 网关（`LITELLM_API_BASE` 的默认值就是
  `http://localhost:4000`）与自建 vLLM 常在同一台机器或内网，一刀切会把项目文档里
  最主要的用法拦掉。

同一份解析结果还用来发现「http + 公网」这种明文 key 场景（只告警，不拒绝），
见 `_warn_plaintext_key_over_public_http`。
"""

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlsplit

from app.config import settings
from app.services.model_service.errors import ProviderFetchError

logger = logging.getLogger(__name__)


def _is_metadata_range(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """链路本地 / 未指定 / 组播：**任何配置下**都不接受的出站目标。

    这一族没有任何「合法的模型网关」用途，却正好覆盖云厂商的实例元数据服务
    （169.254.169.254、fe80:: 链路本地），是最该堵死的那一类。
    """
    return address.is_link_local or address.is_unspecified or address.is_multicast


def _is_internal_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """环回 / 私有 / 保留段：只有开启严格模式时才拒绝。

    `is_global` 是这些类别的并集（还覆盖 100.64/10 这类运营商内网段），比逐个写
    `is_private` / `is_loopback` 更难漏掉一种。**但默认不启用**：本机 LiteLLM 网关
    （`LITELLM_API_BASE` 的默认值就是 `http://localhost:4000`）与自建 vLLM 常在同一台
    机器或内网，一刀切会把项目文档里最主要的用法拦掉。
    """
    return not address.is_global


async def _resolve_addresses(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """把主机名解析成 IP 列表；字面量 IP 直接返回，解析失败返回空列表。

    不能只认字面量：十进制写法（`http://2130706433/`）在解析层等价于 127.0.0.1。
    解析是阻塞调用，放进线程执行。解析失败（域名不存在等）不是内网信号，交给后面的
    连接错误去报，所以这里返回空列表而不是抛错。
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


async def _reject_private_target(base_url: str) -> None:
    """出站前检查目标地址，命中则抛 `ProviderFetchError`。

    调用点覆盖全部出站路径：

    - `create_provider` / `update_provider`：管理员写入时就拒，当场看到 400 与原因，
      而不是等到下一次「测试连接」才发现；
    - `_fetch_remote_models`：「测试连接」与「同步模型」，这两个请求里带明文 API Key；
    - `_build_target`：真正跑对话的出站（`agent_factory.build_provider` 用它构造
      `AsyncOpenAI`）。这一处是必须的——之前只检查了前两处，于是把
      `provider_block_private_targets` 打开后，管理员只要「建提供商 → 建模型 → 发一条
      对话」就能绕过，配置项名不副实。

    两种拒绝强度见模块 docstring。

    已知边界：解析与连接是两次独立的 DNS 查询，中间域名可以变（TOCTOU）。这里不做
    钉 IP 连接，因为那需要绕过 httpx 的 URL 解析、把 TLS SNI 自己接回来，代价远大于
    收益——能利用它必须先控制管理员填写的域名。
    """
    host = urlsplit(base_url).hostname
    if not host:
        # 缺主机名/畸形地址：让 httpx 报它自己的错，不在这里编一个网络错误
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

    只告警不拒绝：内网/本机网关走 http 是文档里的主要用法，一刀切会把它们拦掉；而
    真正让 key 不外流的是「改 Base URL 必须重输 API Key」那条规则。没有 key 的提供商
    根本无明文可泄露，不必打扰管理员。

    只要有一个解析结果是内网地址就不下结论：管理员可能正通过 VPN 或内网域名访问，
    这种情形下给出「明文过公网」的结论本身就是错的。
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
