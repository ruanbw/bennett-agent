"""模型路由目标：数据库配置如何变成一次真实调用。

这条链路的失败模式都很安静：

- 提供商凭据没被用上 → 请求打到了 `.env` 里的旧网关，看起来「能用」，但计费、
  可用模型都错了；
- 管理员改了 Key/Base URL 后装配缓存没有失效 → 后台改了不生效；
- 提供商全部停用后偷偷回退环境变量 → 「停用」没有真实语义。

这里用真实数据库路径（不 mock service）锁住它们。
"""

import pytest
from pydantic_ai.models.fallback import FallbackModel

from app.config import settings
from app.db import AppSetting, async_session_maker
from app.services import model_service
from app.services.agent_factory import build_agent
from app.services.model_target import ModelTarget


async def _seed_provider(monkeypatch, *, name: str = "测试提供商"):
    monkeypatch.setattr(settings, "fallback_model_name", None)
    provider = await model_service.create_provider(
        name=name,
        base_url="http://provider.test:4000/v1",
        api_key="sk-provider-key",
    )
    primary = await model_service.create_provider_model(
        provider.id,
        model_id="primary-model",
        display_name="Primary",
        is_default=True,
        context_window=128_000,
        max_output_tokens=16_384,
    )
    secondary = await model_service.create_provider_model(
        provider.id,
        model_id="secondary-model",
        display_name="Secondary",
    )
    return provider, primary, secondary


async def test_resolve_target_carries_provider_credentials(monkeypatch):
    provider, _, _ = await _seed_provider(monkeypatch)
    key = model_service.make_model_key(provider.slug, "primary-model")

    target = await model_service.resolve_model_target(key)

    assert target is not None
    assert target.key == key
    assert target.upstream_model == "primary-model"
    assert target.base_url == "http://provider.test:4000/v1"
    assert target.api_key == "sk-provider-key"
    assert target.context_window == 128_000
    assert target.max_output_tokens == 16_384
    assert target.fallback is None


async def test_fallback_is_resolved_inside_the_same_provider(monkeypatch):
    provider, _, _ = await _seed_provider(monkeypatch)
    monkeypatch.setattr(settings, "fallback_model_name", "secondary-model")

    key = model_service.make_model_key(provider.slug, "primary-model")
    target = await model_service.resolve_model_target(key)

    assert target is not None
    assert target.fallback is not None
    assert target.fallback.key == model_service.make_model_key(provider.slug, "secondary-model")
    assert target.fallback.base_url == target.base_url
    assert target.fallback.api_key == target.api_key


async def test_disabled_fallback_is_not_used(monkeypatch):
    provider, _, secondary = await _seed_provider(monkeypatch)
    monkeypatch.setattr(settings, "fallback_model_name", "secondary-model")
    await model_service.update_provider_model(secondary.id, enabled=False)

    target = await model_service.resolve_model_target(
        model_service.make_model_key(provider.slug, "primary-model")
    )

    assert target is not None
    assert target.fallback is None


async def test_disabled_provider_makes_its_models_unresolvable(monkeypatch):
    provider, _, _ = await _seed_provider(monkeypatch)
    key = model_service.make_model_key(provider.slug, "primary-model")

    await model_service.update_provider(provider.id, enabled=False)

    assert await model_service.resolve_model_target(key) is None
    assert await model_service.get_default_model_key() is None
    assert await model_service.get_known_model_ids() == set()


async def test_default_key_prefers_explicit_default_then_first_enabled(monkeypatch):
    provider, primary, secondary = await _seed_provider(monkeypatch)

    assert await model_service.get_default_model_key() == model_service.make_model_key(
        provider.slug, "primary-model"
    )

    await model_service.update_provider_model(secondary.id, is_default=True)
    assert await model_service.get_default_model_key() == model_service.make_model_key(
        provider.slug, "secondary-model"
    )

    # 停用默认模型后自动取消默认，回落到第一个启用模型
    await model_service.update_provider_model(secondary.id, enabled=False)
    assert await model_service.get_default_model_key() == model_service.make_model_key(
        provider.slug, "primary-model"
    )


async def test_registry_marker_keeps_database_authoritative_after_deleting_everything(monkeypatch):
    """删光提供商 ≠ 回到环境变量网关。"""
    provider, _, _ = await _seed_provider(monkeypatch)
    await model_service.delete_provider(provider.id)

    async with async_session_maker() as session:
        assert await session.get(AppSetting, model_service.REGISTRY_MARKER_KEY) is not None

    assert await model_service.provider_config_exists() is True
    listed = await model_service.fetch_available_models()
    assert listed.models == []
    assert await model_service.get_default_model_key() is None
    assert await model_service.get_known_model_ids() == set()


def test_agent_uses_the_target_upstream_model_and_base_url():
    target = ModelTarget(
        key="provider-scoped/upstream-model",
        upstream_model="upstream-model",
        provider_name="Provider",
        base_url="http://target-a.test/v1",
        api_key="sk-target-a",
    )

    agent = build_agent(target.key, target=target)

    assert agent.model.model_name == "upstream-model"
    assert str(agent.model.provider.client.base_url).startswith("http://target-a.test/v1")


def test_agent_cache_is_invalidated_when_provider_credentials_change():
    """管理员改了 API Key/地址后必须换一个装配结果，否则「后台改了没生效」。"""
    base = ModelTarget(
        key="cache-test/model",
        upstream_model="model",
        provider_name="Provider",
        base_url="http://cache-a.test/v1",
        api_key="sk-old",
    )
    first = build_agent(base.key, target=base)
    assert build_agent(base.key, target=base) is first

    rotated = ModelTarget(
        key=base.key,
        upstream_model=base.upstream_model,
        provider_name=base.provider_name,
        base_url=base.base_url,
        api_key="sk-new",
    )
    assert build_agent(rotated.key, target=rotated) is not first


def test_fallback_target_is_wrapped_in_fallback_model():
    primary = ModelTarget(
        key="fallback-test/primary",
        upstream_model="primary",
        provider_name="Provider",
        base_url="http://fallback-test.test/v1",
        api_key="sk",
    )
    fallback = ModelTarget(
        key="fallback-test/secondary",
        upstream_model="secondary",
        provider_name="Provider",
        base_url="http://fallback-test.test/v1",
        api_key="sk",
    )
    target = ModelTarget(
        key=primary.key,
        upstream_model=primary.upstream_model,
        provider_name=primary.provider_name,
        base_url=primary.base_url,
        api_key=primary.api_key,
        fallback=fallback,
    )

    agent = build_agent(target.key, target=target)

    assert isinstance(agent.model, FallbackModel)


@pytest.mark.parametrize("enabled", [True, False])
async def test_model_enable_flag_controls_public_list(monkeypatch, enabled):
    provider, primary, _ = await _seed_provider(monkeypatch)
    await model_service.update_provider_model(primary.id, enabled=enabled)

    listed = await model_service.fetch_available_models()
    keys = {item.id for item in listed.models}

    expected = model_service.make_model_key(provider.slug, "primary-model")
    assert (expected in keys) is enabled
