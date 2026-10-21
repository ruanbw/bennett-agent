"""Agent 装配点：R-3（唯一工厂）与 O-2（装配缓存）。"""

import pytest
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIChatModel

from app.config import settings
from app.services import agent_factory
from app.services.agent_factory import (
    build_agent,
    build_title_agent,
    normalize_reasoning_effort,
    reset_agent_cache,
)


@pytest.fixture(autouse=True)
def clear_cache():
    """lru_cache 是模块级状态，用例之间必须隔离。"""
    reset_agent_cache()
    yield
    reset_agent_cache()


# ---------------------------------------------------------------------
# 思考等级归一化
# ---------------------------------------------------------------------
@pytest.mark.parametrize("raw", [None, "", "   "])
def test_blank_effort_means_do_not_send_it(raw):
    assert normalize_reasoning_effort(raw) is None


@pytest.mark.parametrize("raw", ["off", "none", "false", "OFF", "None", " off "])
def test_disabled_aliases_collapse_to_none(raw):
    """各网关接受的关闭写法不同，统一成 "none"。"""
    assert normalize_reasoning_effort(raw) == "none"


@pytest.mark.parametrize("raw", ["disabled", "DISABLED", " disabled "])
def test_disabled_alias_matches_the_frontend(raw):
    """前端 OFF_ALIASES 含 `disabled`，后端漏掉它就会出现「界面显示已关闭、其实还开着」。

    两边词表的对应关系见 `frontend/src/lib/reasoning-effort.ts:OFF_ALIASES`。
    """
    assert normalize_reasoning_effort(raw) == "none"


@pytest.mark.parametrize("raw", ["xhigh", "max", "minimal", " XHIGH "])
def test_unknown_but_well_shaped_levels_pass_through(raw):
    """等级词表由各网关决定，后端只收敛大小写/空白，不擅自丢弃。"""
    assert normalize_reasoning_effort(raw) == raw.strip().lower()


@pytest.mark.parametrize("raw,expected", [("high", "high"), ("HIGH", "high"), (" xhigh ", "xhigh")])
def test_supported_levels_are_lowercased_and_trimmed(raw, expected):
    assert normalize_reasoning_effort(raw) == expected


# ---------------------------------------------------------------------
# 唯一工厂（R-3）
# ---------------------------------------------------------------------
def test_cli_and_web_share_one_factory():
    """CLI 与 Web 必须拿到同一个装配函数。

    改动前 `main.create_agent` 与 `chat_service.get_agent_for_model` 各有一份，
    前者少传 reasoning_effort 与 model_settings，于是同一份配置跑出两个不同 agent。
    """
    import main

    assert not hasattr(main, "create_agent"), "main 不应再自带一份装配逻辑"

    from app.services import chat_service

    assert not hasattr(chat_service, "get_agent_for_model"), "chat_service 不应再自带一份装配逻辑"


def test_build_agent_applies_reasoning_effort():
    agent = build_agent("openai/gpt-4o", "high")

    settings = agent.model.settings
    assert settings["extra_body"]["reasoning_effort"] == "high"


def test_build_agent_omits_effort_when_unspecified():
    """未指定时不传该参数，交给网关默认值而不是自行假设。"""
    agent = build_agent("openai/gpt-4o")

    assert "extra_body" not in (agent.model.settings or {})


def test_build_agent_sends_none_when_explicitly_disabled():
    """显式关闭思考时要发 "none"，而不是干脆不发。

    不带该参数等于「用网关默认值」，而网关默认值很可能是开着的——用户点了
    「关闭思考」却仍然收到思考链。这是原实现的行为，此处锁住。

    断言具体键而不是整字典相等：`model_settings` 里还有上游超时（`timeout`），
    整字典相等会在每次新增一个与本用例无关的设置时无谓地失败。
    """
    for raw in ("off", "none", "false", "OFF"):
        agent = build_agent("openai/gpt-4o", raw)
        assert agent.model.settings["extra_body"] == {"reasoning_effort": "none"}, raw


def test_build_agent_normalises_effort():
    assert build_agent("openai/gpt-4o", "HIGH").model.settings["extra_body"] == {"reasoning_effort": "high"}
    assert build_agent("openai/gpt-4o", " off ").model.settings["extra_body"] == {"reasoning_effort": "none"}


def test_build_agent_registers_configured_tools():
    """生产装配用 `build_tools()`；`ALL_TOOLS` 只是「有哪些工具」的静态全量列表。"""
    from app.tools import build_tools

    agent = build_agent("openai/gpt-4o")

    assert set(agent._function_toolset.tools) == {tool.__name__ for tool in build_tools()}


def test_build_tools_adds_tavily_only_with_a_key(monkeypatch):
    """没有 key 时挂 tavily 只是给模型一个必然失败的工具，白烧一轮 token。

    精确断言整份列表，同时锁住另外两条约定：意图确认（交互协议的一部分）与当前
    时间（纯本地计算）在任何配置下都必须可用。
    """
    from app.tools import build_tools

    monkeypatch.setattr(settings, "tavily_api_key", None)
    assert [tool.__name__ for tool in build_tools()] == ["ask_user_clarification", "get_current_time"]

    monkeypatch.setattr(settings, "tavily_api_key", "tvly-test-key")
    assert [tool.__name__ for tool in build_tools()] == [
        "ask_user_clarification",
        "get_current_time",
        "tavily_search",
    ]


def test_tavily_tool_follows_config_without_restart(monkeypatch):
    """缓存键含 tavily 配置，改配置后无需重启进程就能换掉工具装配。"""
    monkeypatch.setattr(settings, "tavily_api_key", None)
    assert set(build_agent("openai/gpt-4o")._function_toolset.tools) == {
        "ask_user_clarification",
        "get_current_time",
    }

    monkeypatch.setattr(settings, "tavily_api_key", "tvly-test-key")
    assert set(build_agent("openai/gpt-4o")._function_toolset.tools) == {
        "ask_user_clarification",
        "get_current_time",
        "tavily_search",
    }


def test_build_agent_renders_system_prompt():
    from app.config import settings

    agent = build_agent("openai/gpt-4o")
    instructions = "\n".join(agent._system_prompts)

    assert settings.assistant_name in instructions
    assert settings.app_env in instructions


def test_agent_prompt_lists_exactly_the_tools_it_is_built_with():
    """提示词里的工具清单必须与真正挂给 agent 的那份一致，否则模型会调用不存在的工具。"""
    agent = build_agent("openai/gpt-4o")
    instructions = "\n".join(agent._system_prompts)
    mounted = {tool.name for tool in agent._function_toolset.tools.values()}

    for name in mounted:
        assert name in instructions, f"工具 {name} 挂上了却没写进提示词"

    # 没挂 tavily 时提示词不能教模型去调用它
    if "tavily_search" not in mounted:
        assert "请调用 `tavily_search`" not in instructions
        assert "本轮**没有**联网搜索工具" in instructions


# ---------------------------------------------------------------------
# 装配缓存（O-2）
# ---------------------------------------------------------------------
def test_same_args_return_cached_agent():
    """此前每条消息都要重建 provider + model + agent。"""
    assert build_agent("openai/gpt-4o") is build_agent("openai/gpt-4o")


def test_effort_variants_share_one_cache_entry():
    """归一化必须在进缓存之前：否则客户端换个大小写就能把 32 个槽位挤满。"""
    first = build_agent("openai/gpt-4o", "HIGH")

    assert build_agent("openai/gpt-4o", " high ") is first
    assert build_agent("openai/gpt-4o", "high") is first
    assert agent_factory._build_agent_cached.cache_info().currsize == 1


def test_different_args_are_distinct():
    assert build_agent("openai/gpt-4o") is not build_agent("openai/gpt-4o", "high")
    assert build_agent("openai/gpt-4o") is not build_agent("openai/gpt-4o-mini")


def test_cache_is_bounded():
    """客户端能传任意 model 名，缓存必须有上限。"""
    assert agent_factory._build_agent_cached.cache_info().maxsize == agent_factory.AGENT_CACHE_SIZE


@pytest.mark.parametrize(
    "field,value",
    [
        ("litellm_api_base", "http://example.invalid:4000"),
        ("app_env", "staging"),
        ("assistant_name", "小贝"),
        ("model_upstream_timeout", 5.0),
        ("fallback_model_name", "openai/gpt-4o-mini"),
    ],
)
def test_settings_derived_values_are_part_of_the_cache_key(monkeypatch, field, value):
    """改了配置就该换 agent，而不是继续用进程启动时装配的那一个。"""
    first = build_agent("openai/gpt-4o")

    monkeypatch.setattr(settings, field, value)

    assert build_agent("openai/gpt-4o") is not first


# ---------------------------------------------------------------------
# 备用模型（P1）
# ---------------------------------------------------------------------
def test_no_fallback_keeps_the_single_model(monkeypatch):
    # 显式清空：开发者本地 .env.local 若配了备用模型，不该让这条用例变红。
    monkeypatch.setattr(settings, "fallback_model_name", None)

    assert isinstance(build_agent("openai/gpt-4o").model, OpenAIChatModel)


def test_configured_fallback_wraps_the_primary_model(monkeypatch):
    monkeypatch.setattr(settings, "fallback_model_name", "openai/gpt-4o-mini")

    model = build_agent("openai/gpt-4o").model

    assert isinstance(model, FallbackModel)
    assert [inner.model_name for inner in model.models] == ["openai/gpt-4o", "openai/gpt-4o-mini"]


def test_fallback_is_skipped_when_it_names_the_primary_model(monkeypatch):
    """同名兜底只会对同一个模型再打一次，还让错误变成异常组，没有必要。"""
    monkeypatch.setattr(settings, "fallback_model_name", "openai/gpt-4o")

    assert isinstance(build_agent("openai/gpt-4o").model, OpenAIChatModel)


def test_blank_fallback_is_treated_as_disabled(monkeypatch):
    monkeypatch.setattr(settings, "fallback_model_name", "   ")

    assert isinstance(build_agent("openai/gpt-4o").model, OpenAIChatModel)


def test_assistant_name_change_reaches_the_system_prompt(monkeypatch):
    monkeypatch.setattr(settings, "assistant_name", "小贝")

    instructions = "\n".join(build_agent("openai/gpt-4o")._system_prompts)

    assert "小贝" in instructions


def test_title_agent_is_cached_separately():
    assert build_title_agent("openai/gpt-4o") is build_title_agent("openai/gpt-4o")
    assert build_title_agent("openai/gpt-4o") is not build_agent("openai/gpt-4o")


def test_title_agent_has_no_tools():
    """标题生成不该能调工具——那会额外花钱且有副作用。"""
    agent = build_title_agent("openai/gpt-4o")

    assert not agent._function_toolset.tools


def test_reset_agent_cache_drops_instances():
    first = build_agent("openai/gpt-4o")
    reset_agent_cache()

    assert build_agent("openai/gpt-4o") is not first


# ---------------------------------------------------------------------
# 上游超时
#
# 为什么必须有：实测本机的 LiteLLM 代理接受连接但 `/v1/chat/completions` 永不出数据。
# 没有超时的后果是——连接一直被占住、客户端只收到 SSE ping、服务端**一行日志都没有**，
# 生成路径永远不会走到 except 分支，请求无限挂起。
# ---------------------------------------------------------------------
def test_upstream_timeout_is_applied(monkeypatch):
    monkeypatch.setattr(settings, "model_upstream_timeout", 42.0)
    reset_agent_cache()

    timeout = build_agent("openai/gpt-4o").model.settings["timeout"]

    assert timeout.read == 42.0


def test_read_timeout_is_the_configurable_one(monkeypatch):
    """建连/写/取连接用常量，只有「多久没收到数据」可配。

    read 用空闲语义而非总时长，因此正常的长时间流式输出不会被提前掉断。
    """
    monkeypatch.setattr(settings, "model_upstream_timeout", 42.0)
    reset_agent_cache()

    timeout = build_agent("openai/gpt-4o").model.settings["timeout"]

    assert timeout.connect == agent_factory.UPSTREAM_CONNECT_TIMEOUT_SECONDS
    assert timeout.write == agent_factory.UPSTREAM_WRITE_TIMEOUT_SECONDS
    assert timeout.pool == agent_factory.UPSTREAM_POOL_TIMEOUT_SECONDS


@pytest.mark.parametrize("disabled", [0, -1])
def test_zero_disables_the_timeout(monkeypatch, disabled):
    monkeypatch.setattr(settings, "model_upstream_timeout", disabled)
    reset_agent_cache()

    assert agent_factory.build_upstream_timeout() is None
    assert "timeout" not in (build_agent("openai/gpt-4o").model.settings or {})


def test_title_agent_gets_the_timeout_too(monkeypatch):
    """标题生成同样会挂——它也是上游调用。"""
    monkeypatch.setattr(settings, "model_upstream_timeout", 42.0)
    reset_agent_cache()

    assert build_title_agent("openai/gpt-4o").model.settings["timeout"].read == 42.0


def test_timeout_coexists_with_reasoning_effort(monkeypatch):
    monkeypatch.setattr(settings, "model_upstream_timeout", 42.0)
    reset_agent_cache()

    settings_map = build_agent("openai/gpt-4o", "high").model.settings

    assert settings_map["extra_body"] == {"reasoning_effort": "high"}
    assert settings_map["timeout"].read == 42.0


# ---------------------------------------------------------------------
# SDK 重试层
#
# openai SDK 默认 `max_retries=2`，与 120s 空闲超时叠加后单轮最坏可拖 6 分钟。
# 重试交给网关（LiteLLM）统一做，SDK 这一层必须显式关掉。
# ---------------------------------------------------------------------
def test_provider_disables_sdk_level_retries():
    """pydantic-ai 2.31.1 的 LiteLLMProvider 没有 max_retries 参数，只能传现成的客户端。"""
    provider = agent_factory.build_provider()

    assert provider.client.max_retries == 0


def test_title_agent_also_uses_the_no_retry_provider(monkeypatch):
    monkeypatch.setattr(settings, "litellm_api_base", "http://127.0.0.1:9999")
    reset_agent_cache()

    assert build_agent("openai/gpt-4o").model.client.max_retries == 0
    assert build_title_agent("openai/gpt-4o").model.client.max_retries == 0
