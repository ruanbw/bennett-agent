"""规范化 no_proxy 环境变量，避免 httpx 解析出非法代理白名单。

httpx 在 `trust_env=True` 时会把 `no_proxy` 的每一项拼成代理白名单表达式
（见 `httpx._utils.get_environment_proxies`）：IPv4、localhost、域名分别拼成
`all://...`。但它只把**裸** IPv6（`::1`）识别成 IP；方括号写法 `[::1]` 会掉进
域名分支，被拼成 `all://*[::1]` —— 非法 URL。后果是：本进程里任何 httpx 客户端
（模型列表、聊天上游、Tavily）在构造或首次请求时直接抛
`InvalidURL: Invalid port: ':1]'`，请求一个字节都没发出去。

坏的是客户端的代理白名单而不是某一条请求，所以表现为“全挂”：`model_service`
拉不到网关模型列表，静默兜底成配置里的一个模型，最容易被误判成网关或端口问题。

这里做两件事（都只针对「会让 httpx 拼出非法白名单」的条目）：

1. `[IPv6]` / `[IPv6]:端口` 归一化成**裸** `IPv6`（httpx 能正确识别）。带端口的写法
   同样会炸：`[::1]:8000` 被拼成 `all://*[::1]:8000`，报的同样是 `Invalid port: ':1]:8000'`。
   归一化时**丢掉端口**——`no_proxy` 的语义就是按主机名绕过代理，curl/browser 也忽略端口。
2. 其余「含 `[`/`]` 且带冒号」的条目直接丢弃并告警：它们进 httpx 就会让**整个**客户端
   构造失败（`[a:b]` 也被拼成非法 URL），而删掉一个无法解析的条目的代价只是那个主机
   仍走代理——比所有上游请求全挂小得多。

刻意不 `trust_env=False` 一刀切：外网请求（Tavily 等）仍需要继承系统代理。
"""

import ipaddress
import logging
import os
import re
from collections.abc import MutableMapping

logger = logging.getLogger(__name__)

#: 需要检查的两个名字。httpx 通过 urllib.request.getproxies() 读取，两者都会看。
_ENV_NAMES = ("no_proxy", "NO_PROXY")


#: `[IPv6]` 或 `[IPv6]:端口`。
_BRACKETED_IPV6 = re.compile(r"^\[(?P<host>[^\]]+)\](?::(?P<port>[0-9]+))?$")


def _bare_ipv6(token: str) -> str | None:
    """`[::1]` / `[::1]:8000` 转成 `::1`；不是方括号 IPv6 时返回 None。

    带端口的写法丢掉端口：`no_proxy` 是按主机绕过代理，端口不参与匹配
    （裸 `::1:8000` 会被解析成另一个 IPv6 地址，不能简单去掉方括号）。
    """
    matched = _BRACKETED_IPV6.match(token)
    if not matched:
        return None

    inner = matched.group("host")
    try:
        ipaddress.IPv6Address(inner)
    except ValueError:
        return None
    return inner


def _would_break_httpx(token: str) -> bool:
    """这个条目会不会让 httpx 拼出非法白名单。

    httpx 把每个 no_proxy 项拼成 `all://*{条目}`，然后当 URL 解析：条目里只要同时有
    `[`/`]` 与冒号（`[a:b]`、`[::1]`、`[::1]:8000`），端口解析就会失败并让**整个客户端
    构造**抛 `InvalidURL`。不含冒号的 `[not-an-ip]` 能正常解析，保持原样。
    """
    has_bracket = "[" in token or "]" in token
    return has_bracket and ":" in token


def normalize_no_proxy(raw: str) -> str:
    """规范化一个 no_proxy 值；没有会炸 httpx 的条目时原样返回。"""
    tokens: list[str] = []
    changed = False

    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue

        bare = _bare_ipv6(token)
        if bare is not None:
            tokens.append(bare)
            changed = True
            continue

        if _would_break_httpx(token):
            # 解析不了又会让整个客户端构造失败：丢掉它，代价只是这个主机仍走代理
            changed = True
            continue

        tokens.append(token)

    if not changed:
        # 没命中本问题就完全不动原值：空白、大小写等写法差异不属于这里该改的东西
        return raw
    # 解括号后可能与已有的裸写法重复（`::1,[::1]`），保留首次出现、按原顺序去重
    return ",".join(dict.fromkeys(tokens))


def sanitize_no_proxy_env(environ: MutableMapping[str, str] | None = None) -> list[str]:
    """原地规范化 `no_proxy` / `NO_PROXY`，返回被改写的描述（空列表表示没动）。

    幂等：环境已经干净时不会再改、也不会重复告警。默认作用于 `os.environ`；
    测试或调用方可以传入自己的映射。
    """
    env = os.environ if environ is None else environ
    rewrites: list[str] = []

    for name in _ENV_NAMES:
        raw = env.get(name)
        if not raw:
            continue

        normalized = normalize_no_proxy(raw)
        if normalized == raw:
            continue

        env[name] = normalized
        rewrites.append(f"{name}={raw} -> {normalized}")

    if rewrites:
        logger.warning(
            "环境变量 no_proxy 里有 httpx 无法解析的条目（方括号 IPv6 会拼出非法代理白名单，"
            "让所有上游请求在发出前失败）；已归一化为裸 IPv6、无法解析的条目已丢弃: %s",
            "; ".join(rewrites),
        )

    return rewrites
