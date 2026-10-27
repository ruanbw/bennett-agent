"""模型服务里「写入时就要判定」的规则：出站守卫、明文 key 告警、key 重填、并发冲突。

这些规则都发生在**管理员的一次写操作里**，出错方式是「存进去了、某次对话才失败」，
所以只能在服务层直接验证（`app/api/admin.py` 没有 HTTP 层测试，是刻意的现状：路由层
只做异常到状态码的映射）。

- 出站守卫：元数据族地址任何配置下都拒绝，环回/私网默认放行（本机 LiteLLM 网关是
  文档里的主要用法），严格模式才拦内网；公网域名解析失败不当作内网。
- `http://` + 公网地址只**告警**不拒绝：内网/本机网关的 http 是主要用法。
- 换 Base URL 必须重填 API Key，但只对**确实存了 key** 的提供商生效。
- 并发写撞唯一约束必须翻译成可读的服务层错误，而不是 500。

数据库用 conftest 的共享测试库：每个用例前 `TRUNCATE ... RESTART IDENTITY`，因此
「库里有没有提供商 / 有没有 marker」不会随用例顺序漂移。这里仍然替换 `registry.async_session_maker` 指向同一个 maker，
因为注册表在导入时按名字绑定了它——这正是「测试通过但线上没生效」的经典形态。
"""

import socket
import uuid

import pytest
from sqlalchemy import event

from app.config import settings
from app.db import ModelProvider, ProviderModel, async_session_maker
from app.db import engine as app_engine
from app.schemas.chat import ModelListResponse
from app.services import fallback_config, model_service
from app.services.model_service import registry

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


def _loopback_getaddrinfo(host, port):  # noqa: ANN001, ARG001
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]


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


# ---------------------------------------------------------------------
# 每个用例一个临时库
# ---------------------------------------------------------------------
@pytest.fixture
def registry_db(monkeypatch):
    """把模型注册表指向共享测试库，并返回 engine（供 `queries` 监听 SQL）。

    替换的是 `registry.async_session_maker`：注册表在导入时按名字绑定了它，改
    `app.db` 里的同名对象对已绑定的引用无效（这正是「测试通过但线上没生效」的经典形态）。
    """
    monkeypatch.setattr(registry, "async_session_maker", async_session_maker)
    return app_engine


@pytest.fixture
def queries(registry_db):
    """记录注册表在本次用例里发出的 SQL 条数。"""
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, ARG001
        statements.append(statement)

    event.listen(registry_db.sync_engine, "before_cursor_execute", _record)
    return statements


async def _seed_provider(
    registry_db,
    *,
    name="网关",
    api_key="sk-test",
    base_url="http://127.0.0.1:4000/v1",
    slug="gw",
):
    """直接写库造一条提供商；`enabled` 之类的默认值由 ORM 补。"""
    async with async_session_maker() as session:
        provider = ModelProvider(
            name=name, slug=slug, base_url=base_url, api_key=api_key, enabled=True
        )
        session.add(provider)
        await session.commit()
        return provider.id


async def _add_model(registry_db, provider_id, model_id="openai/gpt-4o", **fields):
    async with async_session_maker() as session:
        model = ProviderModel(
            provider_id=provider_id,
            model_id=model_id,
            display_name=fields.pop("display_name", model_id),
            **fields,
        )
        session.add(model)
        await session.commit()
        return model.id


# ---------------------------------------------------------------------
# F3: http + 公网 → 明文 key 告警（只告警，不拒绝）
# ---------------------------------------------------------------------
async def test_plaintext_key_over_public_http_warns(registry_db, monkeypatch, caplog):
    monkeypatch.setattr(model_service.socket, "getaddrinfo", _public_getaddrinfo)

    with caplog.at_level("WARNING", logger="app.services.model_service.outbound_guard"):
        provider = await model_service.create_provider(
            name="公网网关", base_url="http://api.example.com/v1", api_key="sk-secret"
        )

    assert provider.id is not None  # 不拒绝：写入照常成功
    warning = " ".join(record.getMessage() for record in caplog.records)
    assert "明文 http" in warning
    assert "api.example.com" in warning
    # 告警里不能出现密钥原文
    assert "sk-secret" not in warning


async def test_plaintext_key_warning_skips_private_hosts(registry_db, monkeypatch, caplog):
    """内网/本机网关走 http 是文档里的主要用法，不该被提示。"""
    monkeypatch.setattr(model_service.socket, "getaddrinfo", _loopback_getaddrinfo)

    with caplog.at_level("WARNING", logger="app.services.model_service.outbound_guard"):
        await model_service.create_provider(name="本机", base_url="http://gateway.internal:4000/v1", api_key="sk-1")

    assert not caplog.records


async def test_plaintext_key_warning_skips_https_and_keyless_providers(registry_db, monkeypatch, caplog):
    monkeypatch.setattr(model_service.socket, "getaddrinfo", _public_getaddrinfo)

    with caplog.at_level("WARNING", logger="app.services.model_service.outbound_guard"):
        await model_service.create_provider(name="https", base_url="https://api.example.com/v1", api_key="sk-1")
        await model_service.create_provider(name="无凭据", base_url="http://api.example.com/v1", api_key=None)

    assert not caplog.records


async def test_plaintext_key_warning_also_covers_base_url_changes(registry_db, monkeypatch, caplog):
    """更新地址同样会改变 key 的传输路径，告警不能只在 create 里做。"""
    provider_id = await _seed_provider(registry_db, base_url="https://api.example.com/v1")
    monkeypatch.setattr(model_service.socket, "getaddrinfo", _public_getaddrinfo)

    with caplog.at_level("WARNING", logger="app.services.model_service.outbound_guard"):
        await model_service.update_provider(provider_id, base_url="http://api.example.com/v1", api_key="sk-new")

    assert any("明文 http" in record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------------
# F4: 换地址必须重填 key —— 但只对**存了 key** 的提供商
# ---------------------------------------------------------------------
async def test_changing_base_url_requires_api_key_when_provider_has_one(registry_db):
    provider_id = await _seed_provider(registry_db, api_key="sk-existing")

    with pytest.raises(model_service.ApiKeyRequiredError):
        await model_service.update_provider(provider_id, base_url="http://10.0.0.9:4000/v1")

    # 规则是「拦下这次修改」，不是「拦下整个请求」之外的副作用：地址应保持原样
    provider = await model_service.get_provider(provider_id)
    assert provider.base_url == "http://127.0.0.1:4000/v1"


async def test_changing_base_url_is_allowed_for_keyless_provider(registry_db):
    """无凭据的内网网关改地址不该被拦：本来就没有可以泄露给他人的 key。"""
    provider_id = await _seed_provider(registry_db, api_key=None)

    provider = await model_service.update_provider(provider_id, base_url="http://10.0.0.9:4000/v1")

    assert provider is not None
    assert provider.base_url == "http://10.0.0.9:4000/v1"


async def test_changing_base_url_with_new_api_key_is_allowed(registry_db):
    provider_id = await _seed_provider(registry_db, api_key="sk-existing")

    provider = await model_service.update_provider(
        provider_id,
        base_url="http://10.0.0.9:4000/v1",
        api_key="sk-new",
    )
    assert provider is not None
    assert provider.api_key == "sk-new"


async def test_resaving_same_base_url_does_not_require_api_key(registry_db):
    """前端每次保存都会原样回传 base_url，判的是**是否变化**而不是「有没有传」。"""
    provider_id = await _seed_provider(registry_db, api_key="sk-existing")

    provider = await model_service.update_provider(provider_id, base_url="http://127.0.0.1:4000/v1", name="改名")

    assert provider is not None
    assert provider.name == "改名"


# ---------------------------------------------------------------------
# F5: 并发写撞唯一约束 → 服务层可读错误，而不是 500
# ---------------------------------------------------------------------
async def test_concurrent_provider_creation_is_reported_as_fetch_error(registry_db, monkeypatch):
    """两个管理员同时建同名提供商：slug 的「先查后插」双双落空，唯一约束才是裁决者。"""
    await _seed_provider(registry_db, name="默认网关")
    monkeypatch.setattr(registry, "_unique_provider_slug", _always("gw"))

    with pytest.raises(model_service.ProviderFetchError, match="冲突"):
        await model_service.create_provider(name="默认网关", base_url="http://127.0.0.1:5000/v1")


async def test_concurrent_model_creation_is_reported_as_duplicate(registry_db, monkeypatch):
    """`create_provider_model` 的预检查不到（另一边的插入还没提交），只能靠唯一约束兜底。"""
    provider_id = await _seed_provider(registry_db)
    await _add_model(registry_db, provider_id, model_id="openai/gpt-4o")
    monkeypatch.setattr(registry, "_find_duplicate_model_id", _always(None))

    with pytest.raises(model_service.DuplicateModelError):
        await model_service.create_provider_model(provider_id, model_id="openai/gpt-4o")


def _always(value):  # noqa: ANN001, ANN202
    """返回一个恒定返回 `value` 的 async 桩函数。"""

    async def _stub(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        return value

    return _stub


# ---------------------------------------------------------------------
# 一次查询解析一轮对话（白名单 + 目标 + 窗口）
# ---------------------------------------------------------------------
async def test_resolve_chat_model_uses_a_constant_number_of_queries(registry_db, queries):
    """模型数量不影响查询条数：这是「每请求合并成一次查询」的判据。"""
    provider_id = await _seed_provider(registry_db)
    await _add_model(registry_db, provider_id, model_id="openai/gpt-4o", context_window=128000, is_default=True)
    for index in range(5):
        await _add_model(registry_db, provider_id, model_id=f"other/model-{index}")

    queries.clear()
    snapshot = await model_service.resolve_chat_model("gw/openai/gpt-4o")

    assert snapshot.target is not None
    assert snapshot.target.upstream_model == "openai/gpt-4o"
    assert (snapshot.context_window, snapshot.max_output_tokens) == (128000, None)
    # 「有没有 marker」一次 + 「有没有任何提供商」这一次短路查询（marker 缺失时才会
    # 走到）+ 启用模型一次；与模型数量无关，也没有任何 per-model 的追加查询
    assert len(queries) == 3


async def test_resolve_chat_model_falls_back_to_the_default_model(registry_db):
    provider_id = await _seed_provider(registry_db)
    await _add_model(registry_db, provider_id, model_id="openai/first")
    await _add_model(registry_db, provider_id, model_id="openai/second", is_default=True)

    snapshot = await model_service.resolve_chat_model(None)

    assert snapshot.key == "gw/openai/second"


async def test_resolve_chat_model_reports_unknown_key_without_target(registry_db):
    """未知/停用模型：明确给出 `target=None`，调用方据此拒绝（不能退回环境变量）。"""
    provider_id = await _seed_provider(registry_db)
    await _add_model(registry_db, provider_id, model_id="openai/gpt-4o")
    await _add_model(registry_db, provider_id, model_id="openai/retired", enabled=False)

    disabled = await model_service.resolve_chat_model("gw/openai/retired")
    unknown = await model_service.resolve_chat_model("gw/does-not-exist")

    for snapshot in (disabled, unknown):
        assert snapshot.registry_managed is True
        assert snapshot.target is None
        assert snapshot.known_ids == frozenset({"gw/openai/gpt-4o"})


async def test_resolve_chat_model_carries_fallback_window(registry_db, monkeypatch):
    """备用模型接手时用量事件要报**它**的窗口，所以快照里要带上备用模型的窗口。"""
    provider_id = await _seed_provider(registry_db)
    await _add_model(registry_db, provider_id, model_id="openai/primary", context_window=128000)
    await _add_model(registry_db, provider_id, model_id="openai/backup", context_window=32000)
    monkeypatch.setattr(settings, "fallback_model_name", "openai/backup")

    snapshot = await model_service.resolve_chat_model("gw/openai/primary")

    assert snapshot.target.fallback is not None if snapshot.target else False
    assert snapshot.fallback_key == "gw/openai/backup"
    assert snapshot.context_window_for("gw/openai/backup") == 32000
    assert snapshot.context_window_for("gw/openai/primary") == 128000


async def test_resolve_chat_model_gateway_path_fails_closed(registry_db, monkeypatch):
    """旧版网关列表不可用时白名单**收敛为配置模型**，而不是放行任意模型名。

    `registry_db` 是空库（无提供商、无 marker），因此走的正是旧版网关路径。
    此前 `known_ids=None` 意味着上游 `/v1/models` 一抖动，任意登录用户都能让服务端
    用环境变量里的 key 调未启用的模型。
    """
    empty = ModelListResponse(models=[], default_model="openai/gpt-4o")

    async def _unavailable(*, use_cache):  # noqa: ANN001, ARG001
        return empty, True

    monkeypatch.setattr(model_service.gateway, "_load_models", _unavailable)
    monkeypatch.setattr(settings, "model_name", "openai/gpt-4o")
    monkeypatch.setattr(settings, "fallback_model_name", None)

    snapshot = await model_service.resolve_chat_model(None)

    assert snapshot.registry_managed is False
    assert snapshot.target is None
    assert snapshot.known_ids == frozenset({"openai/gpt-4o"})
    assert (snapshot.context_window, snapshot.max_output_tokens) == (None, None)


async def test_resolve_chat_model_gateway_fallback_also_allows_config_fallback(registry_db, monkeypatch):
    """兜底时备用模型名同样要留在白名单里，否则主模型失败后切过去会被 400 挡住。"""
    empty = ModelListResponse(models=[], default_model="openai/gpt-4o")

    async def _unavailable(*, use_cache):  # noqa: ANN001, ARG001
        return empty, True

    monkeypatch.setattr(model_service.gateway, "_load_models", _unavailable)
    monkeypatch.setattr(settings, "model_name", "openai/gpt-4o")
    monkeypatch.setattr(settings, "fallback_model_name", "openai/backup")

    snapshot = await model_service.resolve_chat_model(None)

    assert snapshot.known_ids == frozenset({"openai/gpt-4o", "openai/backup"})


# ---------------------------------------------------------------------
# 对外模型列表：与后台列表的对应关系
#
# 这两个列表本来就**不该**逐行相同（后台要管理停用项，对话只展示可用项），但必须能
# 对上号：聊天框里挑中的那条要能在后台找到对应行。下面锁住的就是「对得上号」的两个
# 前提——暴露上游 model_id，以及启停过滤只发生在对外列表这一侧。
# ---------------------------------------------------------------------
async def test_public_models_expose_upstream_model_id(registry_db):
    """展示名可以重复（同一提供商下多条路由指向同名模型），model_id 是唯一区分依据。

    实测库里就有三条 display_name 都叫 `Deepseek-V4.1-Flash` 的模型
    （`auto/`、`workbuddy/`、`workbuddy-ai/`）。对外列表不带 model_id 时，聊天框里
    几个选项长得一模一样，也没法和后台「展示名 + model_id」的清单对应。
    """
    provider_id = await _seed_provider(registry_db)
    await _add_model(registry_db, provider_id, model_id="auto/deepseek-v4.1-flash", display_name="Deepseek-V4.1-Flash")
    await _add_model(
        registry_db, provider_id, model_id="workbuddy/deepseek-v4.1-flash", display_name="Deepseek-V4.1-Flash"
    )

    result = await model_service.fetch_available_models()

    assert {model.model_id for model in result.models} == {
        "auto/deepseek-v4.1-flash",
        "workbuddy/deepseek-v4.1-flash",
    }
    # 路由键仍然是 {slug}/{model_id}，客户端选中的 id 不变
    assert {model.id for model in result.models} == {
        "gw/auto/deepseek-v4.1-flash",
        "gw/workbuddy/deepseek-v4.1-flash",
    }
    assert len({(model.name, model.provider) for model in result.models}) == 1


async def test_public_list_filters_disabled_models_but_admin_list_keeps_them(registry_db):
    """停用的模型只在对话侧消失，后台仍然要看得到（否则无法重新启用）。"""
    provider_id = await _seed_provider(registry_db)
    await _add_model(registry_db, provider_id, model_id="openai/on")
    await _add_model(registry_db, provider_id, model_id="openai/off", enabled=False)

    public = await model_service.fetch_available_models()
    admin = await model_service.list_provider_models(provider_id)

    assert [model.model_id for model in public.models] == ["openai/on"]
    assert {model.model_id for model in admin} == {"openai/on", "openai/off"}


async def test_public_list_and_known_ids_agree_on_disabled_providers(registry_db):
    """提供商停用时，它的模型既不出现在列表里，也不在接受的白名单里。"""
    provider_id = await _seed_provider(registry_db)
    await _add_model(registry_db, provider_id, model_id="openai/gpt-4o")
    await model_service.update_provider(provider_id, enabled=False)

    result = await model_service.fetch_available_models()

    assert result.models == []
    assert await model_service.get_known_model_ids() == frozenset()


# ---------------------------------------------------------------------
# 备用模型「配了但用不上」：必须留日志
# ---------------------------------------------------------------------
async def test_missing_fallback_model_is_warned(registry_db, monkeypatch, caplog):
    """`FALLBACK_MODEL_NAME` 指向一个没启用的模型时，此前是静默不兜底。

    主模型 503 时用户直接看到失败，而运维以为自己配了兜底——这种「配置看起来生效、
    实际没生效」的问题在故障现场几乎不可能被发现。
    """
    provider_id = await _seed_provider(registry_db)
    await _add_model(registry_db, provider_id, model_id="openai/primary")
    await _add_model(registry_db, provider_id, model_id="openai/backup", enabled=False)
    monkeypatch.setattr(settings, "fallback_model_name", "openai/backup")
    fallback_config.reset_fallback_warnings()

    with caplog.at_level("WARNING", logger="app.services.fallback_config"):
        snapshot = await model_service.resolve_chat_model("gw/openai/primary")

    assert snapshot.target is not None
    assert snapshot.target.fallback is None
    assert snapshot.fallback_key is None
    assert any("备用模型已配置但本轮无法启用" in record.getMessage() for record in caplog.records)


async def test_cross_provider_fallback_is_warned(registry_db, monkeypatch, caplog):
    """备用模型在另一个提供商下：不做跨提供商兜底（参数兼容性无法保证），但要告警。"""
    provider_id = await _seed_provider(registry_db)
    await _add_model(registry_db, provider_id, model_id="openai/primary")
    async with async_session_maker() as session:
        other = ModelProvider(name="另一个", slug="other", base_url="http://127.0.0.1:5000/v1", enabled=True)
        session.add(other)
        await session.commit()
        other_id = other.id
    await _add_model(registry_db, other_id, model_id="openai/backup")
    monkeypatch.setattr(settings, "fallback_model_name", "openai/backup")
    fallback_config.reset_fallback_warnings()

    with caplog.at_level("WARNING", logger="app.services.fallback_config"):
        snapshot = await model_service.resolve_chat_model("gw/openai/primary")

    assert snapshot.fallback_key is None
    assert any("同一提供商内没有这个已启用的模型" in record.getMessage() for record in caplog.records)


async def test_available_fallback_does_not_warn(registry_db, monkeypatch, caplog):
    provider_id = await _seed_provider(registry_db)
    await _add_model(registry_db, provider_id, model_id="openai/primary")
    await _add_model(registry_db, provider_id, model_id="openai/backup")
    monkeypatch.setattr(settings, "fallback_model_name", "openai/backup")
    fallback_config.reset_fallback_warnings()

    with caplog.at_level("WARNING", logger="app.services.fallback_config"):
        snapshot = await model_service.resolve_chat_model("gw/openai/primary")

    assert snapshot.fallback_key == "gw/openai/backup"
    assert not [r for r in caplog.records if "备用模型" in r.getMessage()]


# ---------------------------------------------------------------------
# 用户搜索：LIKE 元字符按字面匹配
# ---------------------------------------------------------------------
async def test_escape_like_pattern_escapes_metacharacters():
    from app.api.admin import escape_like_pattern

    assert escape_like_pattern("%") == "\\%"
    assert escape_like_pattern("a_b") == "a\\_b"
    assert escape_like_pattern("a\\b") == "a\\\\b"
    assert escape_like_pattern("alice") == "alice"


async def _seed_users(registry_db, emails):
    from app.db import User

    async with async_session_maker() as session:
        for email in emails:
            session.add(User(email=email, hashed_password="x", is_active=True, is_verified=True))
        await session.commit()


async def _search(registry_db, query):
    from app.api.admin import list_users

    async with async_session_maker() as session:
        page = await list_users(query=query, limit=50, offset=0, session=session)
    return [item.email for item in page.items]


async def test_user_search_treats_percent_as_a_literal(registry_db):
    """不转义时搜一个 `%` 会列出**全部**账号，看起来像搜索功能坏了。"""
    await _seed_users(registry_db, ["alice@example.com", "bob@example.com"])

    assert await _search(registry_db, "%") == []


async def test_user_search_treats_underscore_as_a_literal(registry_db):
    """邮箱里出现下划线并不罕见：`a_b` 不该命中 `axb`。"""
    await _seed_users(registry_db, ["a_b@example.com", "axb@example.com"])

    assert await _search(registry_db, "a_b") == ["a_b@example.com"]


async def test_user_search_still_does_substring_and_case_insensitive_match(registry_db):
    await _seed_users(registry_db, ["Alice@Example.com", "bob@example.com"])

    assert await _search(registry_db, "alice") == ["Alice@Example.com"]
    assert await _search(registry_db, "example.com") == ["Alice@Example.com", "bob@example.com"]


# ---------------------------------------------------------------------
# 环境变量引导：一条写错的 LITELLM_API_BASE 不该把实例变成「零个可用模型」
# ---------------------------------------------------------------------
def test_validate_bootstrap_base_url_accepts_http_and_strips_trailing_slash():
    from app.services.model_service.registry import validate_bootstrap_base_url

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
    from app.services.model_service.registry import validate_bootstrap_base_url

    url, reason = validate_bootstrap_base_url(raw)

    assert url is None
    assert fragment in reason


async def test_bad_bootstrap_url_keeps_the_env_path_working(registry_db, monkeypatch, caplog):
    """写错的地址不能写进库：一旦写进库，后台就成了权威，实例变成零个可用模型。"""
    from app.config import settings
    from app.services.model_service import registry as reg

    monkeypatch.setattr(settings, "bootstrap_provider_from_env", True)
    monkeypatch.setattr(settings, "litellm_api_base", "localhost:4000")

    with caplog.at_level("WARNING", logger="app.services.model_service.registry"):
        created = await reg.seed_default_provider()

    assert created is False
    # 关键断言：库里没有任何提供商，`_provider_config_exists` 仍为 False，
    # 于是解析继续走环境变量路径（服务不会变成「零个可用模型」）
    assert await reg.provider_config_exists() is False
    assert any("跳过从环境变量引导" in record.getMessage() for record in caplog.records)


async def test_valid_bootstrap_url_still_seeds_a_provider(registry_db, monkeypatch):
    from app.config import settings
    from app.services.model_service import registry as reg

    monkeypatch.setattr(settings, "bootstrap_provider_from_env", True)
    monkeypatch.setattr(settings, "litellm_api_base", "http://127.0.0.1:4000/")
    monkeypatch.setattr(settings, "model_name", "openai/gpt-4o")

    assert await reg.seed_default_provider() is True
    assert await reg.provider_config_exists() is True

    providers = await reg.list_providers()
    assert providers[0].base_url == "http://127.0.0.1:4000"


# ---------------------------------------------------------------------
# 认证头 / 同步落库 raw / 分页 / 厂商预设
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
    assert model_service._auth_headers(bearer) == {"Authorization": "Bearer sk-1"}

    anthropic = provider(api_key="sk-2", auth_style="x-api-key", extra_headers={"anthropic-version": "2023-06-01"})
    assert model_service._auth_headers(anthropic) == {"anthropic-version": "2023-06-01", "x-api-key": "sk-2"}

    goog = provider(api_key="sk-3", auth_style="x-goog-api-key")
    assert model_service._auth_headers(goog) == {"x-goog-api-key": "sk-3"}

    keyless = provider(api_key="sk-4", auth_style="none")
    # none 是「这个网关不收凭据」：即使误存了 key 也不能发出去
    assert model_service._auth_headers(keyless) == {}

    no_key = provider(api_key=None, auth_style="bearer", extra_headers={"x-flag": "1"})
    assert model_service._auth_headers(no_key) == {"x-flag": "1"}


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeClient:
    """记录请求并按序回放响应；`registry.get_http_client` 被替换成返回它。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def get(self, url, headers=None, params=None, timeout=None):  # noqa: ANN001
        self.calls.append({"url": url, "headers": headers, "params": params})
        return self.responses.pop(0)


async def _run_sync(registry_db, monkeypatch, responses):
    provider_id = await _seed_provider(
        registry_db, base_url="http://127.0.0.1:4000/v1", slug=f"sync-{uuid.uuid4().hex[:8]}"
    )
    client = _FakeClient(responses)
    monkeypatch.setattr(registry, "get_http_client", lambda: client)
    result = await model_service.sync_provider_models(provider_id)
    return result, client


async def _load_model_row(registry_db, provider_id, model_id):
    from sqlalchemy import select

    async with async_session_maker() as session:
        row = await session.scalar(
            select(ProviderModel).where(
                ProviderModel.provider_id == provider_id,
                ProviderModel.model_id == model_id,
            )
        )
        assert row is not None
        await session.refresh(row)
        return row


async def test_sync_stores_raw_upstream_entry(registry_db, monkeypatch):
    """同步把上游条目**原样**存进 raw：解析白名单之外的字段（owned_by/created）也留底。"""
    entry = {"id": "openai/gpt-4o", "owned_by": "openai", "context_window": 128000, "created": 1700000000}
    (fetched, created, updated), _ = await _run_sync(
        registry_db, monkeypatch, [_FakeResponse({"data": [entry]})]
    )

    assert (fetched, created, updated) == (1, 1, 0)
    provider_id = (await model_service.list_providers())[0].id
    row = await _load_model_row(registry_db, provider_id, "openai/gpt-4o")
    assert row.raw == entry
    assert row.context_window == 128000


async def test_sync_refreshes_raw_without_touching_enabled(registry_db, monkeypatch):
    """再次同步刷新 raw 与元数据，但管理员调过的 enabled / 默认标记保持不变。"""
    provider_id = await _seed_provider(
        registry_db, base_url="http://127.0.0.1:4000/v1", slug=f"sync-{uuid.uuid4().hex[:8]}"
    )

    first = _FakeClient(
        [_FakeResponse({"data": [{"id": "openai/gpt-4o", "owned_by": "openai", "context_window": 1}]})]
    )
    monkeypatch.setattr(registry, "get_http_client", lambda: first)
    assert await model_service.sync_provider_models(provider_id) == (1, 1, 0)

    row = await _load_model_row(registry_db, provider_id, "openai/gpt-4o")
    await model_service.update_provider_model(row.id, enabled=False)

    new_entry = {"id": "openai/gpt-4o", "owned_by": "openai", "context_window": 999, "note": "changed"}
    second = _FakeClient([_FakeResponse({"data": [new_entry]})])
    monkeypatch.setattr(registry, "get_http_client", lambda: second)
    assert await model_service.sync_provider_models(provider_id) == (1, 0, 1)

    row = await _load_model_row(registry_db, provider_id, "openai/gpt-4o")
    assert row.raw == new_entry
    assert row.context_window == 999
    assert row.enabled is False


async def test_sync_follows_anthropic_pagination(registry_db, monkeypatch):
    """has_more + last_id 驱动翻页；第二页请求要带上游标。"""
    page1 = {"data": [{"id": "claude-a"}], "has_more": True, "last_id": "claude-a"}
    page2 = {"data": [{"id": "claude-b"}], "has_more": False, "last_id": "claude-b"}

    (fetched, created, _), client = await _run_sync(
        registry_db, monkeypatch, [_FakeResponse(page1), _FakeResponse(page2)]
    )

    assert fetched == 2
    assert created == 2
    assert len(client.calls) == 2
    assert client.calls[0]["params"] is None
    assert client.calls[1]["params"] == {"after_id": "claude-a"}


async def test_sync_rejects_unrecognized_envelope(registry_db, monkeypatch):
    """非 OpenAI/原生/裸数组的信封要给出可读错误，而不是静默同步出 0 个模型。"""
    with pytest.raises(model_service.ProviderFetchError, match="响应格式"):
        await _run_sync(registry_db, monkeypatch, [_FakeResponse({"models_count": 5})])


async def test_create_provider_with_preset_applies_auth(registry_db, monkeypatch):
    """预设创建：地址与认证方式从预设**抄进库**，preset_key 留痕。

    断言对齐目录条目而不是硬编码值：这里要验证的是「复制」这个行为，目录内容
    （bearer 还是 x-api-key）本身由文档调研决定，改目录不该连带改这个用例。
    """
    from app.services.model_service.presets import get_preset

    monkeypatch.setattr(model_service.socket, "getaddrinfo", _public_getaddrinfo)

    provider = await model_service.create_provider(name="Claude", api_key="sk-x", preset_key="anthropic")
    preset = get_preset("anthropic")

    assert provider.base_url == preset.base_url
    assert provider.auth_style == preset.auth_style
    assert provider.extra_headers == preset.extra_headers
    assert provider.preset_key == "anthropic"


async def test_create_provider_with_preset_base_url_override(registry_db, monkeypatch):
    """预设 + 显式地址：地址用管理员给的（代理/镜像），认证方式仍按预设。"""
    monkeypatch.setattr(model_service.socket, "getaddrinfo", _public_getaddrinfo)

    provider = await model_service.create_provider(
        name="OpenAI 代理",
        base_url="http://127.0.0.1:9000/v1",
        preset_key="openai",
    )

    assert provider.base_url == "http://127.0.0.1:9000/v1"
    assert provider.auth_style == "bearer"
    assert provider.preset_key == "openai"


async def test_create_provider_unknown_preset_is_rejected(registry_db):
    with pytest.raises(model_service.ProviderFetchError, match="未知的提供商预设"):
        await model_service.create_provider(name="x", preset_key="does-not-exist")


async def test_create_provider_without_preset_or_url_is_rejected(registry_db):
    with pytest.raises(model_service.ProviderFetchError, match="Base URL"):
        await model_service.create_provider(name="x")


# ---------------------------------------------------------------------
# 用户列表按状态筛选：账号一多，「有哪些被封了」不能靠翻页找人
# ---------------------------------------------------------------------
async def _seed_mixed_users(registry_db):
    from app.db import User

    async with async_session_maker() as session:
        session.add_all(
            [
                User(email="alice@example.com", hashed_password="x", is_active=True, is_verified=True),
                User(email="bob@example.com", hashed_password="x", is_active=False, is_verified=True),
                User(email="carol@example.com", hashed_password="x", is_active=False, is_verified=False),
            ]
        )
        await session.commit()


async def _list_users_page(registry_db, *, query=None, status="all"):
    from app.api.admin import list_users

    async with async_session_maker() as session:
        return await list_users(
            query=query,
            status_filter=status,
            limit=50,
            offset=0,
            session=session,
        )


async def test_user_list_can_filter_banned_accounts(registry_db):
    await _seed_mixed_users(registry_db)

    banned = await _list_users_page(registry_db, status="banned")
    active = await _list_users_page(registry_db, status="active")

    assert [item.email for item in banned.items] == ["bob@example.com", "carol@example.com"]
    assert [item.email for item in active.items] == ["alice@example.com"]


async def test_status_filter_applies_to_the_total_not_just_the_page(registry_db):
    """`total` 要是筛选后的总数：否则分页器会指向一个不存在的页数。"""
    await _seed_mixed_users(registry_db)

    page = await _list_users_page(registry_db, status="banned")

    assert page.total == 2
    assert page.limit == 50
    assert page.offset == 0


async def test_status_filter_combines_with_search(registry_db):
    await _seed_mixed_users(registry_db)

    page = await _list_users_page(registry_db, query="carol", status="banned")
    mismatch = await _list_users_page(registry_db, query="alice", status="banned")

    assert [item.email for item in page.items] == ["carol@example.com"]
    assert mismatch.items == []
    assert mismatch.total == 0


async def test_status_all_keeps_every_account(registry_db):
    await _seed_mixed_users(registry_db)

    page = await _list_users_page(registry_db)

    assert page.total == 3
    assert [item.email for item in page.items] == [
        "alice@example.com",
        "bob@example.com",
        "carol@example.com",
    ]


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
        await model_service._reject_private_target(base_url)
    assert "链路本地" in str(excinfo.value)


async def test_nat64_loopback_is_internal_in_strict_mode(monkeypatch):
    """64:ff9b::7f00:1 外层是 global，严格模式也必须按内嵌 127.0.0.1 拦下。"""
    monkeypatch.setattr(settings, "provider_block_private_targets", True)
    with pytest.raises(model_service.ProviderFetchError) as excinfo:
        await model_service._reject_private_target("http://[64:ff9b::7f00:1]/v1")
    assert "内网" in str(excinfo.value)


async def test_nat64_public_embedded_address_is_allowed(monkeypatch):
    """NAT64 包装的公网地址不应被误杀（严格模式下也放行）。"""
    monkeypatch.setattr(settings, "provider_block_private_targets", True)
    await model_service._reject_private_target("http://[64:ff9b::808:808]/v1")


async def test_resolve_chat_model_carries_declared_reasoning_levels(registry_db):
    """思考等级校验需要模型声明的词表；快照必须把它带出来（并归一化成小写）。"""
    provider_id = await _seed_provider(registry_db)
    await _add_model(
        registry_db,
        provider_id,
        model_id="openai/thinker",
        supported_reasoning_levels=["low", "HIGH", " max "],
    )

    snapshot = await model_service.resolve_chat_model("gw/openai/thinker")

    assert snapshot.reasoning_levels == frozenset({"low", "high", "max"})


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


# ---------------------------------------------------------------------
# 超管降级的并发守卫（L-12）
# ---------------------------------------------------------------------
async def _role_users(registry_db, **specs):
    """按 `email=role` 造用户，返回 {email: id}。"""
    from app.db import User

    ids: dict[str, object] = {}
    async with async_session_maker() as session:
        for email, role in specs.items():
            user = User(
                email=email,
                hashed_password="x",
                is_active=True,
                is_verified=True,
                role=role,
                is_superuser=role in ("admin", "superadmin"),
            )
            session.add(user)
            await session.flush()
            ids[email] = user.id
        await session.commit()
    return ids


async def test_demoting_the_last_superadmin_is_rejected_atomically(registry_db, monkeypatch):
    """「先 count 再写」在并发下会双双通过；单条带子查询的 UPDATE 必须兜住最后一个。"""
    from fastapi import HTTPException
    from sqlalchemy import func, select

    from app.api.admin import set_user_role
    from app.db import User
    from app.schemas.admin import RoleUpdate

    async def _noop(_user_id):  # noqa: ANN001, ANN202
        return 0

    monkeypatch.setattr("app.api.admin.delete_user_access_tokens", _noop)
    ids = await _role_users(registry_db, **{"caller@example.com": "admin", "target@example.com": "superadmin"})

    async with async_session_maker() as session:
        caller = await session.get(User, ids["caller@example.com"])
        with pytest.raises(HTTPException) as excinfo:
            await set_user_role(ids["target@example.com"], RoleUpdate(role="user"), caller, session)
        assert excinfo.value.status_code == 400

    async with async_session_maker() as session:
        remaining = await session.scalar(select(func.count()).select_from(User).where(User.role == "superadmin"))
    assert remaining == 1


async def test_superadmin_can_be_demoted_when_another_one_remains(registry_db, monkeypatch):
    from sqlalchemy import select

    from app.api.admin import set_user_role
    from app.db import User
    from app.schemas.admin import RoleUpdate

    async def _noop(_user_id):  # noqa: ANN001, ANN202
        return 0

    monkeypatch.setattr("app.api.admin.delete_user_access_tokens", _noop)
    ids = await _role_users(registry_db, **{"caller@example.com": "superadmin", "target@example.com": "superadmin"})

    async with async_session_maker() as session:
        caller = await session.get(User, ids["caller@example.com"])
        result = await set_user_role(ids["target@example.com"], RoleUpdate(role="user"), caller, session)
        assert result.role == "user"

    async with async_session_maker() as session:
        target = await session.get(User, ids["target@example.com"])
        assert target.role == "user" and target.is_superuser is False
        assert await session.scalar(select(User.role).where(User.id == ids["caller@example.com"])) == "superadmin"
