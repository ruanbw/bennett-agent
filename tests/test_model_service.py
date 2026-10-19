"""模型列表解析与兜底（`app/services/model_service.py` 的纯函数）。

这里的关键契约有两个：

1. 兜底**不伪造思考能力**——它返回 `reasoning=False` + 空等级，从而让前端不展示
   思考等级选择器。前端侧对应 `frontend/src/lib/reasoning-effort.js:levelsFor`，
   两边必须一致，否则又会出现「用户选了却请求失败」。
2. `reasoning` 是三态：True/False 只来自网关的明确表态，缺字段/类型不符是 None
   （未知），不能被压成 False。

上游交互、缓存与模型白名单的用例已随全量/端到端层移除；这里只保留直接调用即可
验证的解析与容错逻辑。
"""

import logging

import pytest

from app.services.model_service import (
    _fallback,
    _parse_models,
    _parse_reasoning_levels,
)


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
