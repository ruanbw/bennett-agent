"""模型目标 → 装配结果（`build_agent` 的纯装配逻辑）。

提供商凭据 / 地址变化必须换一个装配结果（否则「后台改了没生效」），备用模型要包成
`FallbackModel`。数据库解析路径（`resolve_model_target`、提供商 CRUD）的用例已随
全量/端到端层移除。
"""

from pydantic_ai.models.fallback import FallbackModel

from app.services.agent_factory import build_agent
from app.services.model_target import ModelTarget


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
