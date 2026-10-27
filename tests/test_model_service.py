"""模型列表解析与兜底（`app/services/model_service/` 的纯函数）。

这里的关键契约有三个：

1. 兜底**不伪造思考能力**——它返回 `reasoning=False` + 空等级，从而让前端不展示
   思考等级选择器。前端侧对应 `frontend/src/lib/reasoning-effort.ts:levelsFor`，
   两边必须一致，否则又会出现「用户选了却请求失败」。
2. `reasoning` 是三态：True/False 只来自网关的明确表态，缺字段/类型不符是 None
   （未知），不能被压成 False。
3. 多厂商解析**只认别名表、不猜**：窗口/输出上限解析不出就留 None，绝不编一个数；
   原始条目原样保留在 `UpstreamModel.raw`，供后台回答「上游到底报了什么」。

上游交互、缓存与模型白名单的用例已随全量/端到端层移除；这里只保留直接调用即可
验证的解析与容错逻辑。
"""

import logging

import pytest

from app.services.model_service import (
    _build_models_url,
    _fallback,
    _parse_models,
    _parse_reasoning_levels,
    next_page_cursor,
    parse_upstream_models,
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


# ---------------------------------------------------------------------
# 多厂商信封与字段别名（upstream.parse_upstream_models）
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        {"data": [{"id": "a/one"}]},  # OpenAI 系（绝大多数厂商）
        {"models": [{"id": "a/one"}]},  # Gemini / Ollama 原生
        [{"id": "a/one"}],  # Together 的裸数组
    ],
)
def test_envelope_variants(payload):
    assert [entry.model_id for entry in parse_upstream_models(payload, "x")] == ["a/one"]


def test_raw_entry_is_preserved_verbatim():
    """原始条目是同步白名单之外信息的唯一去处，必须原封不动。"""
    raw = {
        "id": "openai/gpt-4o",
        "owned_by": "openai",
        "created": 1700000000,
        "pricing": {"prompt": "0.0001"},
    }

    entry = parse_upstream_models({"data": [raw]}, "magpie_label")[0]

    assert entry.raw == raw
    assert entry.raw is raw


def test_openrouter_style_entry():
    """OpenRouter：name 是展示名、top_provider 里是实际生效的上限、能力靠 supported_parameters。"""
    raw = {
        "id": "openai/gpt-4o",
        "name": "OpenAI: GPT-4o",
        "description": "GPT-4o via OpenRouter",
        "context_length": 128000,
        "top_provider": {"context_length": 111000, "max_completion_tokens": 16384},
        "supported_parameters": ["tools", "reasoning"],
        "pricing": {"prompt": "0.0001"},
    }

    entry = parse_upstream_models({"data": [raw]}, "magpie_label")[0]

    assert entry.model_id == "openai/gpt-4o"
    assert entry.display_name == "OpenAI: GPT-4o"
    assert entry.description == "GPT-4o via OpenRouter"
    # top_provider 比自报的 context_length 更保守，预算用它更安全
    assert entry.context_window == 111000
    assert entry.max_output_tokens == 16384
    assert entry.reasoning is True


def test_groq_style_entry():
    raw = {"id": "llama-3.3-70b", "owned_by": "meta", "context_window": 131072, "max_completion_tokens": 32768}

    entry = parse_upstream_models({"data": [raw]}, "magpie_label")[0]

    assert (entry.context_window, entry.max_output_tokens) == (131072, 32768)
    assert entry.reasoning is None  # Groq 没给思考信息，不能编


def test_anthropic_style_entry():
    """Anthropic：窗口叫 max_input_tokens、输出叫 max_tokens、思考在 capabilities 里。"""
    raw = {
        "type": "model",
        "id": "claude-sonnet-4-5",
        "display_name": "Claude Sonnet 4.5",
        "max_input_tokens": 200000,
        "max_tokens": 64000,
        "capabilities": {"thinking": True},
    }

    entry = parse_upstream_models({"data": [raw]}, "magpie_label")[0]

    assert (entry.display_name, entry.context_window, entry.max_output_tokens, entry.reasoning) == (
        "Claude Sonnet 4.5",
        200000,
        64000,
        True,
    )


def test_gemini_native_entry_strips_models_prefix():
    """Gemini 原生：信封是 models、id 带 `models/` 前缀、字段是驼峰。"""
    raw = {
        "name": "models/gemini-2.5-pro",
        "displayName": "Gemini 2.5 Pro",
        "description": "desc",
        "inputTokenLimit": 1048576,
        "outputTokenLimit": 65536,
        "thinking": True,
    }

    entry = parse_upstream_models({"models": [raw]}, "magpie_label")[0]

    assert (entry.model_id, entry.display_name) == ("gemini-2.5-pro", "Gemini 2.5 Pro")
    assert (entry.context_window, entry.max_output_tokens, entry.reasoning) == (1048576, 65536, True)


def test_ollama_tags_entry():
    """Ollama 原生：id 在 name/model，思考在 capabilities 枚举数组里。"""
    raw = {
        "name": "qwen3:8b",
        "model": "qwen3:8b",
        "details": {"parameter_size": "8.2B"},
        "capabilities": ["completion", "thinking"],
    }

    entry = parse_upstream_models({"models": [raw]}, "magpie_label")[0]

    assert entry.model_id == "qwen3:8b"
    assert entry.reasoning is True
    assert entry.context_window is None  # /api/tags 不报窗口，留空而不是猜


def test_ark_and_mistral_aliases():
    ark = parse_upstream_models(
        {"data": [{"id": "ep-1", "token_limits": {"context_window": 256000, "max_output_token_length": 32768}}]},
        "x",
    )[0]
    assert (ark.context_window, ark.max_output_tokens) == (256000, 32768)

    mistral = parse_upstream_models(
        {"data": [{"id": "mistral-large", "name": "Mistral Large", "max_context_length": 131072}]},
        "x",
    )[0]
    assert (mistral.display_name, mistral.context_window) == ("Mistral Large", 131072)


def test_reasoning_is_never_derived_false():
    """能力表里没列 thinking 不等于不支持：列表不完整是常态。"""
    entry = parse_upstream_models(
        {"data": [{"id": "a/one", "capabilities": {"vision": True}, "supported_parameters": ["tools"]}]},
        "x",
    )[0]

    assert entry.reasoning is None


def test_unparseable_window_is_none_not_fabricated():
    """别名表之外/解析不出的字段一律 None——预算会拿这个数砍请求，编造比缺失危险。"""
    entry = parse_upstream_models({"data": [{"id": "a/one", "context_window": "128k", "max_tokens": True}]}, "x")[0]

    assert entry.context_window is None
    assert entry.max_output_tokens is None


def test_build_models_url_versioned_bases():
    """路径里已有版本段的地址直接拼 /models；裸主机才补 /v1/models。"""
    assert _build_models_url("http://localhost:4000") == "http://localhost:4000/v1/models"
    assert _build_models_url("https://api.openai.com/v1") == "https://api.openai.com/v1/models"
    assert _build_models_url("https://open.bigmodel.cn/api/paas/v4") == "https://open.bigmodel.cn/api/paas/v4/models"
    assert (
        _build_models_url("https://generativelanguage.googleapis.com/v1beta/openai")
        == "https://generativelanguage.googleapis.com/v1beta/openai/models"
    )
    assert (
        _build_models_url("https://ark.cn-beijing.volces.com/api/v3")
        == "https://ark.cn-beijing.volces.com/api/v3/models"
    )


def test_next_page_cursor_only_for_anthropic_shape():
    """游标只认 has_more + last_id；认错形状等于对不分页的上游反复重打第一页。"""
    assert next_page_cursor({"data": [], "has_more": True, "last_id": "claude-a"}) == "claude-a"
    assert next_page_cursor({"data": [], "has_more": False, "last_id": "claude-a"}) is None
    assert next_page_cursor({"data": [], "has_more": True}) is None  # 没有 last_id，翻不了页
    assert next_page_cursor({"data": [{"id": "x"}]}) is None
    assert next_page_cursor([{"id": "x"}]) is None
    assert next_page_cursor(None) is None
