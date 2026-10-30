"""模型服务里「写入时就要判定」的规则：出站守卫、明文 key 告警、key 重填、并发冲突。

这些规则都发生在**管理员的一次写操作里**，出错方式是「存进去了、某次对话才失败」，
所以只能在服务层直接验证（`app/api/admin.py` 没有 HTTP 层测试，是刻意的现状：路由层
只做异常到状态码的映射）。

- 出站守卫：元数据族地址任何配置下都拒绝，环回/私网默认放行（本机 LiteLLM 网关是
  文档里的主要用法），严格模式才拦内网；公网域名解析失败不当作内网。
- `http://` + 公网地址只**告警**不拒绝：内网/本机网关的 http 是主要用法。
- 换 Base URL 必须重填 API Key，但只对**确实存了 key** 的提供商生效。
- 并发写撞唯一约束必须翻译成可读的服务层错误，而不是 500。

**零基础设施**：本文件不连数据库、不开任何外部服务。写库相关的用例（换 key、并发写、
同步落库、用户搜索/筛选、模型解析快照）已随「测试环境零基础设施」改造移除；
这里只保留**直接调用纯函数即可判定**的规则。
"""

import socket

import pytest

from app.config import settings
from app.services import model_service
from app.services.model_service import provider_sync
from app.services.model_service.outbound_guard import _warn_plaintext_key_over_public_http

# ---------------------------------------------------------------------
# 出站目标地址检查（API Key 不能送到元数据服务／任意主机）
# ---------------------------------------------------------------------


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
        await model_service.reject_private_target(base_url)
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
    await model_service.reject_private_target(base_url)


@pytest.mark.parametrize(
    "base_url",
    ["http://127.0.0.1:3425/v1", "http://localhost:4000/v1", "http://10.0.0.1:8000/v1"],
)
async def test_private_targets_are_rejected_in_strict_mode(monkeypatch, base_url):
    monkeypatch.setattr(settings, "provider_block_private_targets", True)
    with pytest.raises(model_service.ProviderFetchError) as excinfo:
        await model_service.reject_private_target(base_url)
    assert "内网" in str(excinfo.value)


def _public_getaddrinfo(host, port):  # noqa: ANN001, ARG001
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


def _loopback_getaddrinfo(host, port):  # noqa: ANN001, ARG001
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]


@pytest.mark.parametrize(
    "base_url",
    ["https://api.deepseek.com/v1", "http://8.8.8.8:9999/v1", "http://93.184.216.34/v1"],
)
async def test_public_targets_are_allowed(monkeypatch, base_url):
    # 公网域名不该真的去解析：用例只关心这条规则是否放行
    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    await model_service.reject_private_target(base_url)


async def test_dns_failure_is_not_treated_as_an_internal_target(monkeypatch):
    def _unresolvable(host, port):  # noqa: ANN001, ARG001
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _unresolvable)
    # 不抛：解析失败交给后面的连接错误，而不是在这里报「内网」
    await model_service.reject_private_target("https://provider.invalid/v1")


# ---------------------------------------------------------------------
# F3: http + 公网 → 明文 key 告警（只告警，不拒绝）
#
# 告警判定本身不查库：直接调守卫层函数，不需要先写一条提供商。
# ---------------------------------------------------------------------
async def test_plaintext_key_over_public_http_warns(monkeypatch, caplog):
    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)

    with caplog.at_level("WARNING", logger="app.services.model_service.outbound_guard"):
        await _warn_plaintext_key_over_public_http(
            "http://api.example.com/v1",
            provider_name="公网网关",
            has_api_key=True,
        )

    warning = " ".join(record.getMessage() for record in caplog.records)
    assert "明文 http" in warning
    assert "api.example.com" in warning
    # 告警里不能出现密钥原文
    assert "sk-secret" not in warning


async def test_plaintext_key_warning_skips_private_hosts(monkeypatch, caplog):
    """内网/本机网关走 http 是文档里的主要用法，不该被提示。"""
    monkeypatch.setattr(socket, "getaddrinfo", _loopback_getaddrinfo)

    with caplog.at_level("WARNING", logger="app.services.model_service.outbound_guard"):
        await _warn_plaintext_key_over_public_http(
            "http://gateway.internal:4000/v1",
            provider_name="本机",
            has_api_key=True,
        )

    assert not caplog.records


async def test_plaintext_key_warning_skips_https_and_keyless_providers(monkeypatch, caplog):
    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)

    with caplog.at_level("WARNING", logger="app.services.model_service.outbound_guard"):
        # https：本来就是加密通道
        await _warn_plaintext_key_over_public_http(
            "https://api.example.com/v1",
            provider_name="https",
            has_api_key=True,
        )
        # 无凭据：没有 key 可以泄露
        await _warn_plaintext_key_over_public_http(
            "http://api.example.com/v1",
            provider_name="无凭据",
            has_api_key=False,
        )

    assert not caplog.records


# ---------------------------------------------------------------------
# F4: 换地址必须重填 key —— 只对**确实存了 key** 的提供商生效
# ---------------------------------------------------------------------
async def test_create_provider_rejects_unknown_preset():
    """未知预设要报可读错误（400），而不是落一条半截的提供商。"""
    with pytest.raises(model_service.ProviderFetchError, match="未知的提供商预设"):
        await model_service.create_provider(name="x", preset_key="does-not-exist")


async def test_create_provider_without_preset_or_url_is_rejected():
    with pytest.raises(model_service.ProviderFetchError, match="Base URL"):
        await model_service.create_provider(name="x")


# ---------------------------------------------------------------------
# 用户搜索：LIKE 元字符按字面匹配
# ---------------------------------------------------------------------
def test_escape_like_pattern_escapes_metacharacters():
    from app.api.admin import escape_like_pattern

    assert escape_like_pattern("%") == "\\%"
    assert escape_like_pattern("a_b") == "a\\_b"
    assert escape_like_pattern("a\\b") == "a\\\\b"
    assert escape_like_pattern("alice") == "alice"


# ---------------------------------------------------------------------
# 环境变量引导：一条写错的 LITELLM_API_BASE 不该把实例变成「零个可用模型」
# ---------------------------------------------------------------------
def test_validate_bootstrap_base_url_accepts_http_and_strips_trailing_slash():
    from app.services.model_service.provider_sync import validate_bootstrap_base_url

    assert validate_bootstrap_base_url("http://localhost:4000/") == ("http://localhost:4000", "")
    assert validate_bootstrap_base_url("  https://api.example.com/v1  ") == ("https://api.example.com/v1", "")


@pytest.mark.parametrize(
    "raw,fragment",
    [
        ("", "为空"),
        ("   ", "为空"),
        ("localhost:4000", "http://"),
        ("ftp://gateway/v1", "http://"),
        ("http://", "缺少主机名"),
        ("http://169.254.169.254/latest", "链路本地"),
    ],
)
def test_validate_bootstrap_base_url_rejects_bad_values(raw, fragment):
    from app.services.model_service.provider_sync import validate_bootstrap_base_url

    url, reason = validate_bootstrap_base_url(raw)

    assert url is None
    assert fragment in reason


# ---------------------------------------------------------------------
# 认证头 / 厂商预设
# ---------------------------------------------------------------------
def test_auth_headers_follow_auth_style():
    """API Key 放进哪个头由库里的 auth_style 决定；固定头原样合并。"""
    from app.db import ModelProvider

    def provider(**fields):
        fields.setdefault("name", "p")
        fields.setdefault("slug", "p")
        fields.setdefault("base_url", "http://127.0.0.1:4000/v1")
        return ModelProvider(**fields)

    bearer = provider(api_key="sk-1", auth_style="bearer")
    assert provider_sync.build_auth_headers(bearer) == {"Authorization": "Bearer sk-1"}

    anthropic = provider(api_key="sk-2", auth_style="x-api-key", extra_headers={"anthropic-version": "2023-06-01"})
    assert provider_sync.build_auth_headers(anthropic) == {"anthropic-version": "2023-06-01", "x-api-key": "sk-2"}

    goog = provider(api_key="sk-3", auth_style="x-goog-api-key")
    assert provider_sync.build_auth_headers(goog) == {"x-goog-api-key": "sk-3"}

    keyless = provider(api_key="sk-4", auth_style="none")
    # none 是「这个网关不收凭据」：即使误存了 key 也不能发出去
    assert provider_sync.build_auth_headers(keyless) == {}

    no_key = provider(api_key=None, auth_style="bearer", extra_headers={"x-flag": "1"})
    assert provider_sync.build_auth_headers(no_key) == {"x-flag": "1"}


# ---------------------------------------------------------------------
# 内嵌 IPv4 / NAT64：只看外层地址会漏掉元数据与环回
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "base_url",
    [
        # IPv6 内嵌 IPv4：外层 is_global=True，实际会连到内嵌的元数据/环回地址
        "http://[64:ff9b::a9fe:a9fe]/latest/meta-data",
        "http://[::a9fe:a9fe]/latest/meta-data",
        # 云厂商元数据不全是 link-local
        "http://100.100.100.200/latest/meta-data",
        "http://[fd00:ec2::254]/latest/meta-data",
    ],
)
async def test_embedded_and_cloud_metadata_targets_are_rejected(base_url):
    with pytest.raises(model_service.ProviderFetchError) as excinfo:
        await model_service.reject_private_target(base_url)
    assert "链路本地" in str(excinfo.value)


async def test_nat64_loopback_is_internal_in_strict_mode(monkeypatch):
    """64:ff9b::7f00:1 外层是 global，严格模式也必须按内嵌 127.0.0.1 拦下。"""
    monkeypatch.setattr(settings, "provider_block_private_targets", True)
    with pytest.raises(model_service.ProviderFetchError) as excinfo:
        await model_service.reject_private_target("http://[64:ff9b::7f00:1]/v1")
    assert "内网" in str(excinfo.value)


async def test_nat64_public_embedded_address_is_allowed(monkeypatch):
    """NAT64 包装的公网地址不应被误杀（严格模式下也放行）。"""
    monkeypatch.setattr(settings, "provider_block_private_targets", True)
    await model_service.reject_private_target("http://[64:ff9b::808:808]/v1")


# ---------------------------------------------------------------------
# 思考等级校验：模型没声明等级时不能凭空拒绝
# ---------------------------------------------------------------------
def test_validate_reasoning_effort_rejects_undeclared_levels():
    """前端不应发未声明等级；手工构造的请求也不能把任意值塞进 extra_body。"""
    import pytest as _pytest
    from fastapi import HTTPException

    from app.api.chat import validate_reasoning_effort
    from app.services.model_service import ChatModelSnapshot

    snapshot = ChatModelSnapshot(
        key="gw/thinker",
        target=None,
        context_window=None,
        max_output_tokens=None,
        fallback_key=None,
        fallback_context_window=None,
        known_ids=frozenset({"gw/thinker"}),
        registry_managed=True,
        reasoning_levels=frozenset({"low", "high"}),
    )

    validate_reasoning_effort(snapshot, None)
    validate_reasoning_effort(snapshot, "HIGH")
    validate_reasoning_effort(snapshot, "off")  # 关闭思考始终允许

    with _pytest.raises(HTTPException) as excinfo:
        validate_reasoning_effort(snapshot, "ultra")
    assert excinfo.value.status_code == 400


def test_validate_reasoning_effort_is_skipped_when_model_does_not_declare_levels():
    """网关没枚举等级（很多 OpenAI 兼容网关）时不能凭空拒绝。"""
    from app.api.chat import validate_reasoning_effort
    from app.services.model_service import ChatModelSnapshot

    snapshot = ChatModelSnapshot(
        key="gw/unknown",
        target=None,
        context_window=None,
        max_output_tokens=None,
        fallback_key=None,
        fallback_context_window=None,
        known_ids=frozenset({"gw/unknown"}),
        registry_managed=True,
    )

    validate_reasoning_effort(snapshot, "whatever")


@pytest.mark.parametrize("field,value", [("context_window", 1), ("max_output_tokens", 1), ("context_window", 2**31)])
def test_model_limits_reject_absurd_values(field, value):
    """窗口/输出上限是**有量纲**的，不能只校验「正整数」。

    填 1：`build_budget` 扣掉系统提示词开销（约 1.5k token）后 `limit_tokens=0`，
    该模型下每一条对话都以 `context_overflow` 收尾，文案是「超过可用上限 0」——管理员
    不会认为这是自己填错了一个数字。

    填 2^31：列是 PG `INTEGER`，asyncpg 抛 DataError → 未捕获 → 500。
    """
    from pydantic import ValidationError

    from app.schemas.admin import ModelCreate, ModelUpdate

    with pytest.raises(ValidationError):
        ModelCreate(model_id="openai/x", **{field: value})
    with pytest.raises(ValidationError):
        ModelUpdate(**{field: value})
