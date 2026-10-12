"""模型列表服务：解析、兜底、缓存并发与模型白名单。

这里的关键契约有两个：

1. 兜底**不伪造思考能力**——它返回 `reasoning=False` + 空等级，从而让前端不展示
   思考等级选择器。前端侧对应 `frontend/src/lib/reasoning-effort.js:levelsFor`，
   两边必须一致，否则又会出现「用户选了却请求失败」。
2. `reasoning` 是三态：True/False 只来自网关的明确表态，缺字段/类型不符是 None
   （未知），不能被压成 False。
"""

import asyncio
import logging

import pytest

from app.config import settings
from app.services import model_service
from app.services.model_service import (
    _fallback,
    _parse_models,
    _parse_reasoning_levels,
    fetch_available_models,
    get_known_model_ids,
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


class GatedClient(FakeClient):
    """第一次 get 会挂起，直到测试放行——用来观察并发请求是否被合并。"""

    def __init__(self, response=None, exc=None):
        super().__init__(response=response, exc=exc)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def get(self, *_args, **_kwargs):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        if self.exc is not None:
            raise self.exc
        return self.response


@pytest.fixture
def fake_http(monkeypatch):
    """替换共享客户端，并记录调用次数（用于验证缓存）。"""

    def install(response=None, exc=None, client_class=FakeClient):
        client = client_class(response=response, exc=exc)
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


def test_reasoning_levels_ignore_blank_and_non_string_effort():
    """`{"effort": null}` 经 str() 会变成等级 "None"，整行都该丢掉。"""
    assert _parse_reasoning_levels([{"effort": None}, {"effort": 5}, {"effort": "  "}, {"effort": "high"}]) == ["high"]


def test_reasoning_levels_are_stripped_and_deduplicated():
    assert _parse_reasoning_levels([" high ", "high", {"effort": "low"}, "low"]) == ["high", "low"]


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
    assert model.reasoning is True


def test_missing_default_falls_back_to_first_model(caplog):
    """配置的默认模型不在网关列表里时，前端仍需有一个可选项。"""
    payload = {"data": [{"id": "a/one"}, {"id": "b/two"}]}

    with caplog.at_level(logging.INFO, logger="app.services.model_service"):
        result = _parse_models(payload, "missing/model", "magpie_label")

    assert result is not None
    assert result.default_model == "a/one"
    assert result.models[0].is_default is True
    assert result.models[1].is_default is False
    # 静默改选会让排障失去线索，日志里要能看到「为什么不是配置的那个」
    assert "missing/model" in caplog.text
    assert "a/one" in caplog.text


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


# ---------------------------------------------------------------------
# 条目级容错（坏条目只丢自己）
# ---------------------------------------------------------------------
def test_bad_entry_does_not_discard_the_whole_list(caplog):
    """回归：此前任一条目抛 ValidationError 都会被外层吞掉，整份列表只剩兜底模型。"""
    payload = {
        "data": [
            {"id": 123},
            {"id": "a/one"},
            {"id": "b/two", "display_name": 5},
            {"id": "c/three"},
        ]
    }

    with caplog.at_level(logging.WARNING, logger="app.services.model_service"):
        result = _parse_models(payload, "a/one", "x")

    assert result is not None
    assert [m.id for m in result.models] == ["a/one", "c/three"]
    assert result.default_model == "a/one"
    # 日志要能定位到具体条目：序号 + id
    assert "index=0" in caplog.text
    assert "index=2" in caplog.text
    assert "b/two" in caplog.text


def test_int_like_strings_are_parsed():
    payload = {"data": [{"id": "a/one", "context_window": "200000", "max_output_tokens": " 8192 "}]}

    model = _parse_models(payload, "a/one", "x").models[0]

    assert model.context_window == 200000
    assert model.max_output_tokens == 8192


@pytest.mark.parametrize("value", ["128k", "abc", None, 1.5, True, [128000]])
def test_unparseable_int_fields_are_none_not_fabricated(value):
    """不做单位换算也不猜：前端会拿这个数字做判断，编一个比空着更糟。"""
    payload = {"data": [{"id": "a/one", "context_window": value, "max_output_tokens": value}]}

    model = _parse_models(payload, "a/one", "x").models[0]

    assert model.context_window is None
    assert model.max_output_tokens is None


@pytest.mark.parametrize(
    "payload",
    [{}, {"data": "not a list"}, {"data": []}, [], None, "nope", {"data": [{"display_name": "x"}]}],
)
def test_unusable_payloads_return_none(payload):
    assert _parse_models(payload, "openai/gpt-4o", "x") is None


# ---------------------------------------------------------------------
# reasoning 三态
# ---------------------------------------------------------------------
def test_reasoning_is_none_when_gateway_omits_the_field():
    """「网关没说」不能当成「明确不支持」，否则前端会白白隐藏思考等级选择器。"""
    model = _parse_models({"data": [{"id": "a/one"}]}, "a/one", "x").models[0]

    assert model.reasoning is None


def test_reasoning_false_is_preserved():
    model = _parse_models({"data": [{"id": "a/one", "reasoning": False}]}, "a/one", "x").models[0]

    assert model.reasoning is False


@pytest.mark.parametrize("value", ["false", "true", 0, 1, None])
def test_reasoning_non_bool_is_unknown(value):
    model = _parse_models({"data": [{"id": "a/one", "reasoning": value}]}, "a/one", "x").models[0]

    assert model.reasoning is None


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


async def test_failures_use_short_negative_cache_ttl(fake_http, monkeypatch):
    """兜底响应采用短 TTL (10s)，避免上游抖动锁死 5 分钟。"""
    clock = {"now": 1000.0}
    monkeypatch.setattr(model_service.time, "monotonic", lambda: clock["now"])

    client = fake_http(FakeResponse(status_code=503))
    await fetch_available_models()
    assert client.calls == 1

    # 5 秒内在短 TTL 内，不重试
    clock["now"] += 5.0
    await fetch_available_models()
    assert client.calls == 1

    # 11 秒超过 10s 短 TTL，应该重新打上游
    clock["now"] += 6.0
    await fetch_available_models()
    assert client.calls == 2


async def test_parse_exception_falls_back_and_caches_short(fake_http, monkeypatch):
    """_parse_models 抛出异常时捕获、打印 warning 并走短 TTL 兜底。"""

    def broken_parse(*_args, **_kwargs):
        raise TypeError("unexpected structure")

    monkeypatch.setattr(model_service, "_parse_models", broken_parse)
    client = fake_http(FakeResponse(payload={"data": [{"id": "a/one"}]}))

    result = await fetch_available_models()
    assert result == _fallback(settings.model_name)
    assert client.calls == 1


# ---------------------------------------------------------------------
# single-flight：并发首拉只打一次上游
# ---------------------------------------------------------------------
async def test_concurrent_first_fetch_hits_upstream_once(fake_http):
    """回归：TTL 过期后 N 个并发请求此前会各打一次上游。"""
    client = fake_http(FakeResponse(payload={"data": [{"id": "a/one"}]}), client_class=GatedClient)

    tasks = [asyncio.create_task(fetch_available_models()) for _ in range(5)]
    await client.started.wait()
    # 给其余任务充分机会去（错误地）打上游
    for _ in range(10):
        await asyncio.sleep(0)
    assert client.calls == 1

    client.release.set()
    results = await asyncio.gather(*tasks)

    assert client.calls == 1
    assert all(result == results[0] for result in results)


async def test_concurrent_callers_share_the_failure_result_too(fake_http):
    """上游挂了时并发首拉同样只打一次，其余请求吃负缓存。"""
    client = fake_http(FakeResponse(status_code=503), client_class=GatedClient)

    tasks = [asyncio.create_task(fetch_available_models()) for _ in range(4)]
    await client.started.wait()
    for _ in range(10):
        await asyncio.sleep(0)
    assert client.calls == 1

    client.release.set()
    results = await asyncio.gather(*tasks)

    assert client.calls == 1
    assert all(result == _fallback(settings.model_name) for result in results)


async def test_failure_does_not_overwrite_successful_cache(fake_http):
    """回归：慢失败的兜底结果此前会覆盖刚拿到的成功列表。"""
    fake_http(FakeResponse(payload={"data": [{"id": "a/one"}]}))
    success = await fetch_available_models()
    assert [m.id for m in success.models] == ["a/one"]

    # 上游开始抖动，一次强制刷新失败 → 返回兜底，但成功结果必须留在缓存里
    fake_http(FakeResponse(status_code=503))
    assert await fetch_available_models(use_cache=False) == _fallback(settings.model_name)

    assert await fetch_available_models() == success


async def test_success_replaces_negative_cache(fake_http):
    fake_http(FakeResponse(status_code=503))
    assert await fetch_available_models() == _fallback(settings.model_name)

    fake_http(FakeResponse(payload={"data": [{"id": "a/one"}]}))
    refreshed = await fetch_available_models(use_cache=False)
    assert [m.id for m in refreshed.models] == ["a/one"]

    # 负缓存已清掉，后续请求继续拿成功结果
    assert await fetch_available_models() == refreshed


# ---------------------------------------------------------------------
# 模型白名单
# ---------------------------------------------------------------------
async def test_known_model_ids_come_from_gateway(fake_http):
    fake_http(FakeResponse(payload={"data": [{"id": "a/one"}, {"id": "b/two"}]}))

    assert await get_known_model_ids() == {"a/one", "b/two"}


async def test_known_model_ids_is_none_when_gateway_unavailable(fake_http):
    """拿不到网关列表时必须返回 None（放行），不能拿兜底模型去否决用户的选择。"""
    fake_http(FakeResponse(status_code=500))

    assert await get_known_model_ids() is None


async def test_known_model_ids_is_none_for_empty_gateway_list(fake_http):
    fake_http(FakeResponse(payload={"data": []}))

    assert await get_known_model_ids() is None


async def test_chat_endpoint_rejects_model_outside_gateway_list(monkeypatch):
    """端点接线：白名单非 None 时，显式传入的未知模型直接 400。"""
    from fastapi import HTTPException

    from app.api import chat as chat_api
    from app.schemas.chat import ChatMessage, ChatRequest

    async def known_ids():
        return {"a/one"}

    monkeypatch.setattr(chat_api, "get_known_model_ids", known_ids)
    request = ChatRequest(model="expensive/model", messages=[ChatMessage(role="user", content="你好")])

    with pytest.raises(HTTPException) as excinfo:
        await chat_api.chat_stream(request)

    assert excinfo.value.status_code == 400
    assert "不在可用模型列表" in excinfo.value.detail


async def test_chat_endpoint_accepts_whitelisted_model(monkeypatch):
    from app.api import chat as chat_api
    from app.schemas.chat import ChatMessage, ChatRequest

    async def known_ids():
        return {"a/one"}

    monkeypatch.setattr(chat_api, "get_known_model_ids", known_ids)
    request = ChatRequest(model="a/one", messages=[ChatMessage(role="user", content="你好")])

    # 只构造响应对象，不迭代生成器（迭代会去真的调模型）
    assert await chat_api.chat_stream(request) is not None


async def test_title_endpoint_checks_whitelist(monkeypatch):
    from fastapi import HTTPException

    from app.api import chat as chat_api
    from app.schemas.chat import TitleGenerateRequest

    async def known_ids():
        return {"a/one"}

    monkeypatch.setattr(chat_api, "get_known_model_ids", known_ids)

    with pytest.raises(HTTPException) as excinfo:
        await chat_api.generate_title(TitleGenerateRequest(model="expensive/model", prompt="你好"))

    assert excinfo.value.status_code == 400


async def test_model_is_allowed_when_gateway_list_unavailable(monkeypatch):
    """无法校验时放行：网关抖动不该让对话功能跟着不可用。"""
    from app.api import chat as chat_api
    from app.schemas.chat import ChatMessage, ChatRequest

    async def unknown_ids():
        return None

    monkeypatch.setattr(chat_api, "get_known_model_ids", unknown_ids)
    request = ChatRequest(model="whatever/model", messages=[ChatMessage(role="user", content="你好")])

    assert await chat_api.chat_stream(request) is not None
