"""模型列表服务：解析、兜底与缓存。

这里的关键契约是 `model_service` 的兜底**不伪造思考能力**——它返回
`reasoning=False` + 空等级，从而让前端不展示思考等级选择器。前端侧对应
`frontend/src/lib/reasoning-effort.js:levelsFor`，两边必须一致，否则又会出现
「用户选了却请求失败」。
"""

import pytest

from app.config import settings
from app.services import model_service
from app.services.model_service import (
    _fallback,
    _parse_models,
    _parse_reasoning_levels,
    fetch_available_models,
    reset_models_cache,
)


@pytest.fixture(autouse=True)
def clear_cache():
    reset_models_cache()
    yield
    reset_models_cache()


class FakeResponse:
    def __init__(self, status_code=200, payload=None, raise_on_json=False):
        self.status_code = status_code
        self._payload = payload
        self._raise_on_json = raise_on_json

    def json(self):
        if self._raise_on_json:
            raise ValueError("not json")
        return self._payload


class FakeClient:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = 0

    async def get(self, *_args, **_kwargs):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.response


@pytest.fixture
def fake_http(monkeypatch):
    """替换共享客户端，并记录调用次数（用于验证缓存）。"""

    def install(response=None, exc=None):
        client = FakeClient(response=response, exc=exc)
        monkeypatch.setattr(model_service, "get_http_client", lambda: client)
        return client

    return install


# ---------------------------------------------------------------------
# 思考等级解析
# ---------------------------------------------------------------------
def test_reasoning_levels_accept_object_shape():
    assert _parse_reasoning_levels([{"effort": "high"}, {"effort": "low"}]) == ["high", "low"]


def test_reasoning_levels_accept_string_shape():
    assert _parse_reasoning_levels(["high", "low"]) == ["high", "low"]


def test_reasoning_levels_ignore_unknown_shapes():
    """伪造等级会让用户选了之后请求失败。"""
    assert _parse_reasoning_levels([1, None, {"other": "x"}, "high"]) == ["high"]


@pytest.mark.parametrize("raw", [None, "high", 42, {"a": 1}])
def test_reasoning_levels_tolerate_non_list(raw):
    assert _parse_reasoning_levels(raw) == []


# ---------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------
def test_parses_gateway_payload():
    payload = {
        "data": [
            {
                "id": "openai/gpt-4o",
                "display_name": "GPT-4o",
                "owned_by": "openai",
                "reasoning": True,
                "supported_reasoning_levels": [{"effort": "high"}],
                "context_window": 128000,
            }
        ]
    }

    result = _parse_models(payload, "openai/gpt-4o", "magpie_label")

    assert result is not None
    assert result.default_model == "openai/gpt-4o"
    model = result.models[0]
    assert model.is_default is True
    assert model.name == "GPT-4o"
    assert model.provider == "openai"
    assert model.supported_reasoning_levels == ["high"]
    assert model.context_window == 128000


def test_missing_default_falls_back_to_first_model():
    """配置的默认模型不在网关列表里时，前端仍需有一个可选项。"""
    payload = {"data": [{"id": "a/one"}, {"id": "b/two"}]}

    result = _parse_models(payload, "missing/model", "magpie_label")

    assert result is not None
    assert result.default_model == "a/one"
    assert result.models[0].is_default is True
    assert result.models[1].is_default is False


def test_description_field_is_configurable():
    """字段名属于网关细节，由配置传入，不硬编码在模块里。"""
    payload = {"data": [{"id": "a/one", "vendor_label": "厂商标签"}]}

    assert _parse_models(payload, "a/one", "vendor_label").models[0].description == "厂商标签"
    # 未提供展示名时回退为 'provider id'（沿袭原有行为，即使看着有点冗余）
    assert _parse_models(payload, "a/one", "absent_field").models[0].description == "a a/one"


def test_description_falls_back_to_provider_and_display_name():
    payload = {"data": [{"id": "openai/gpt-4o", "display_name": "GPT-4o"}]}

    assert _parse_models(payload, "openai/gpt-4o", "absent").models[0].description == "openai GPT-4o"


def test_derives_provider_from_id_when_absent():
    payload = {"data": [{"id": "openai/gpt-4o"}]}

    assert _parse_models(payload, "openai/gpt-4o", "x").models[0].provider == "openai"


def test_provider_is_custom_without_namespace():
    payload = {"data": [{"id": "plain-model"}]}

    assert _parse_models(payload, "plain-model", "x").models[0].provider == "Custom"


def test_skips_entries_without_id():
    payload = {"data": [{"display_name": "no id"}, {"id": "a/one"}]}

    assert len(_parse_models(payload, "a/one", "x").models) == 1


@pytest.mark.parametrize(
    "payload",
    [{}, {"data": "not a list"}, {"data": []}, [], None, "nope", {"data": [{"display_name": "x"}]}],
)
def test_unusable_payloads_return_none(payload):
    assert _parse_models(payload, "openai/gpt-4o", "x") is None


# ---------------------------------------------------------------------
# 兜底
# ---------------------------------------------------------------------
def test_fallback_does_not_fabricate_reasoning():
    """刻意不伪造：前端据此隐藏思考等级选择器。"""
    fallback = _fallback("openai/gpt-4o")
    model = fallback.models[0]

    assert model.reasoning is False
    assert model.supported_reasoning_levels == []
    assert model.is_default is True


def test_fallback_splits_namespace():
    model = _fallback("openai/gpt-4o").models[0]

    assert (model.provider, model.name) == ("openai", "gpt-4o")


def test_fallback_without_namespace():
    model = _fallback("plain-model").models[0]

    assert (model.provider, model.name) == ("Custom", "plain-model")


# ---------------------------------------------------------------------
# 上游交互与缓存
# ---------------------------------------------------------------------
async def test_successful_fetch(fake_http):
    fake_http(FakeResponse(payload={"data": [{"id": "a/one"}]}))

    result = await fetch_available_models()

    assert [m.id for m in result.models] == ["a/one"]


async def test_non_200_falls_back(fake_http):
    fake_http(FakeResponse(status_code=500))

    assert await fetch_available_models() == _fallback(settings.model_name)


async def test_unreachable_gateway_falls_back(fake_http):
    import httpx

    fake_http(exc=httpx.ConnectError("boom"))

    assert await fetch_available_models() == _fallback(settings.model_name)


async def test_non_json_body_falls_back(fake_http):
    fake_http(FakeResponse(raise_on_json=True))

    assert await fetch_available_models() == _fallback(settings.model_name)


async def test_empty_model_list_falls_back(fake_http):
    fake_http(FakeResponse(payload={"data": []}))

    assert await fetch_available_models() == _fallback(settings.model_name)


async def test_second_call_is_served_from_cache(fake_http):
    """每次页面加载都打上游既慢又浪费。"""
    client = fake_http(FakeResponse(payload={"data": [{"id": "a/one"}]}))

    first = await fetch_available_models()
    second = await fetch_available_models()

    assert client.calls == 1
    assert first == second


async def test_cache_can_be_bypassed(fake_http):
    client = fake_http(FakeResponse(payload={"data": [{"id": "a/one"}]}))

    await fetch_available_models()
    await fetch_available_models(use_cache=False)

    assert client.calls == 2


async def test_failures_are_also_cached(fake_http):
    """上游挂了时缓存兜底结果，避免每次请求都去撞一遍超时。"""
    client = fake_http(FakeResponse(status_code=503))

    await fetch_available_models()
    await fetch_available_models()

    assert client.calls == 1
