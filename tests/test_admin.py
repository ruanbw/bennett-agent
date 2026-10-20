"""模型服务里「写入时就要判定」的规则：出站守卫、明文 key 告警、key 重填、并发冲突。

这些规则都发生在**管理员的一次写操作里**，出错方式是「存进去了、某次对话才失败」，
所以只能在服务层直接验证（`app/api/admin.py` 没有 HTTP 层测试，是刻意的现状：路由层
只做异常到状态码的映射）。

- 出站守卫：元数据族地址任何配置下都拒绝，环回/私网默认放行（本机 LiteLLM 网关是
  文档里的主要用法），严格模式才拦内网；公网域名解析失败不当作内网。
- `http://` + 公网地址只**告警**不拒绝：内网/本机网关的 http 是主要用法。
- 换 Base URL 必须重填 API Key，但只对**确实存了 key** 的提供商生效。
- 并发写撞唯一约束必须翻译成可读的服务层错误，而不是 500。

数据库一律用每个用例自己的临时 SQLite 文件（替换 `registry.async_session_maker`）：
共用会话库会让「库里有没有提供商」「有没有 marker」随用例顺序漂移。
"""

import socket

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db import Base, ModelProvider, ProviderModel
from app.schemas.chat import ModelListResponse
from app.services import model_service
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
async def registry_db(tmp_path, monkeypatch):
    """把模型注册表指向一个全新的 SQLite 文件，并返回它的 engine。

    替换的是 `registry.async_session_maker`：注册表在导入时按名字绑定了它，改
    `app.db` 里的同名对象对已绑定的引用无效（这正是「测试通过但线上没生效」的经典形态）。
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'registry.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(registry, "async_session_maker", async_sessionmaker(engine, expire_on_commit=False))
    yield engine
    await engine.dispose()


@pytest.fixture
def queries(registry_db):
    """记录注册表在本次用例里发出的 SQL 条数。"""
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001, ARG001
        statements.append(statement)

    event.listen(registry_db.sync_engine, "before_cursor_execute", _record)
    return statements


async def _seed_provider(registry_db, *, name="网关", api_key="sk-test", base_url="http://127.0.0.1:4000/v1"):
    """直接写库造一条提供商；`enabled` 之类的默认值由 ORM 补。"""
    maker = async_sessionmaker(registry_db, expire_on_commit=False)
    async with maker() as session:
        provider = ModelProvider(name=name, slug="gw", base_url=base_url, api_key=api_key, enabled=True)
        session.add(provider)
        await session.commit()
        return provider.id


async def _add_model(registry_db, provider_id, model_id="openai/gpt-4o", **fields):
    maker = async_sessionmaker(registry_db, expire_on_commit=False)
    async with maker() as session:
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


async def test_resolve_chat_model_gateway_path_is_fail_open(registry_db, monkeypatch):
    """旧版网关列表不可用时白名单**放行**（`known_ids=None`），不是空集合。

    `registry_db` 是空库（无提供商、无 marker），因此走的正是旧版网关路径。
    """
    empty = ModelListResponse(models=[], default_model="openai/gpt-4o")

    async def _unavailable(*, use_cache):  # noqa: ANN001, ARG001
        return empty, True

    monkeypatch.setattr(model_service.gateway, "_load_models", _unavailable)

    snapshot = await model_service.resolve_chat_model(None)

    assert snapshot.registry_managed is False
    assert snapshot.target is None
    assert snapshot.known_ids is None
    assert (snapshot.context_window, snapshot.max_output_tokens) == (None, None)


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
