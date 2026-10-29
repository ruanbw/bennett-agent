"""规范化 no_proxy：`[::1]` 会被 httpx 拼成 `all://*[::1]`，客户端一构造就抛 `InvalidURL`。

方括号 IPv6 归一化成裸地址并丢掉端口（no_proxy 不按端口匹配），仍会炸的条目丢弃并告警；
不设 `trust_env=False`，因为外网请求仍要继承系统代理。
"""

import ipaddress
import logging
import os
import re
from collections.abc import MutableMapping

logger = logging.getLogger(__name__)

#: 需要检查的两个名字。httpx 通过 urllib.request.getproxies() 读取，两者都会看。
_ENV_NAMES = ("no_proxy", "NO_PROXY")


#: 形如 `192.168.0.0/16` 或 `fd00::/8` 的网段条目。
_CIDR = re.compile(r"^(?P<host>[^/\s]+)/(?P<len>[0-9]{1,3})$")

#: `[IPv6]` 或 `[IPv6]:端口`。
_BRACKETED_IPV6 = re.compile(r"^\[(?P<host>[^\]]+)\](?::(?P<port>[0-9]+))?$")


def _bare_ipv6(token: str) -> str | None:
    """`[::1]` / `[::1]:8000` 转成 `::1`；不是方括号 IPv6 时返回 None。

    端口必须丢：裸 `::1:8000` 会被解析成另一个 IPv6 地址，不能简单去掉方括号。
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
    """这个条目会不会让 httpx 拼出非法白名单（每项被拼成 `all://*<条目>` 当 URL 解析）。

    同时带 `[`/`]` 与冒号的条目会让端口解析失败、整个客户端构造抛 `InvalidURL`；
    `[not-an-ip]` 能正常解析，保持原样。
    """
    has_bracket = "[" in token or "]" in token
    return has_bracket and ":" in token


def unsupported_entries(raw: str) -> list[str]:
    """`no_proxy` 里看起来像网段的条目。

    httpx 的白名单是 URL 模式（`all://*<条目>`），没有网段语义：`192.168.0.0/16` 永远不匹配
    真实主机，不报错只是默默不生效，而写它的人几乎都想「内网直连」。改不出来，只能告警。
    """
    entries: list[str] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        matched = _CIDR.match(token)
        if not matched:
            continue
        host, length = matched.group("host"), int(matched.group("len"))
        # 只认「主机部分是个 IP、前缀长度合法」的写法；`example.com/8` 这种滑稽输入不报
        try:
            ipaddress.ip_address(host)
        except ValueError:
            continue
        if host and 0 <= length <= (128 if ":" in host else 32):
            entries.append(token)
    return entries


#: 已经告警过的网段写法。`get_http_client()` 每次新建客户端都会调本模块，不去重会刷屏。
_WARNED_CIDR: set[str] = set()


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

    幂等：环境已经干净时不会再改、也不会重复告警。默认作用于 `os.environ`，测试可注入映射。
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

    # 网段条目：不改变值（改了也不会更好），但必须让人知道它不生效
    for name in _ENV_NAMES:
        raw = env.get(name)
        if not raw:
            continue
        for entry in unsupported_entries(raw):
            if entry in _WARNED_CIDR:
                continue
            _WARNED_CIDR.add(entry)
            logger.warning(
                "环境变量 %s 里的网段条目 %r 不会被 httpx 识别（白名单是 URL 模式，没有网段语义），"
                "这些地址仍会走代理。若目的是内网直连，请改成逐条列出具体主机名或 IP。",
                name,
                entry,
            )

    if rewrites:
        logger.warning(
            "环境变量 no_proxy 里有 httpx 无法解析的条目（方括号 IPv6 会拼出非法代理白名单，"
            "让所有上游请求在发出前失败）；已归一化为裸 IPv6、无法解析的条目已丢弃: %s",
            "; ".join(rewrites),
        )

    return rewrites
