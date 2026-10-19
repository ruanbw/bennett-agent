"""CLI 与 Web 的守卫对齐（`main.py`）。

CLI 不是"另一套实现"，它和 Web 共用同一条装配链（`agent_factory`）与同一份预算
计算（`token_budget`）。历史差异是**偶然**长出来的：CLI 直接 `agent.run_stream`，
既没有历史上限、也没有 token 预算，于是「本地跑得通、Web 却拒」——而两边用的是
同一个模型、同一份提示词。

这里锁两件事：

1. 预算口径一致：同样的历史，CLI 与 Web 算出的结论必须相同；
2. 单一事件循环：Agent 里的 httpx 客户端不能跨 loop 复用。
"""

import asyncio

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

import main
from app.config import settings
from app.services.token_budget import (
    TOOL_SCHEMA_OVERHEAD_TOKENS,
    build_budget,
    estimate_agent_messages_tokens,
    estimate_tokens,
)


class _FakeEngine:
    """`AsyncEngine.dispose` 是只读方法，只能替换整个 engine 对象。"""

    def __init__(self, dispose):
        self._dispose = dispose

    async def dispose(self):
        await self._dispose()


def _history(chars: int) -> list:
    """造一段相当于 `chars` 个中文字符的历史。"""
    return [
        ModelRequest(parts=[UserPromptPart(content="你" * chars)]),
        ModelResponse(parts=[TextPart(content="好")]),
    ]


def test_estimate_counts_text_thinking_and_tool_parts():
    """所有会占窗口的部件都要计入——漏一类就是系统性低估。"""
    from pydantic_ai.messages import ThinkingPart, ToolCallPart, ToolReturnPart

    messages = [
        ModelRequest(parts=[UserPromptPart(content="问题" * 100)]),
        ModelResponse(parts=[ThinkingPart(content="思考" * 100), TextPart(content="答案" * 100)]),
        ModelRequest(parts=[ToolReturnPart(tool_name="t", content="工具结果" * 100, tool_call_id="1")]),
        ModelResponse(parts=[ToolCallPart(tool_name="t", args={"q": "参数" * 100}, tool_call_id="2")]),
    ]

    total = estimate_agent_messages_tokens(messages)

    # 四段各 200 个中文字符 = 800 tokens，加工具参数与结构性开销
    assert total >= 800
    assert total == estimate_agent_messages_tokens(messages)  # 稳定


def test_estimate_handles_empty_history():
    assert estimate_agent_messages_tokens([]) == 0


def test_budget_over_limit_matches_web_rules():
    """同样的窗口与历史，CLI 的 `_budget_for_cli` 与 Web 的 `build_budget` 同结论。"""
    history = _history(40_000)  # 4 万中文字符 ≈ 4 万 token

    cli_budget = main._budget_for_cli("openai/gpt-4o", history, context_window=8_192, max_output=1_024)

    assert cli_budget is not None
    # 与 Web 侧同样的口径手工重算一次
    expected = build_budget(
        "openai/gpt-4o",
        8_192,
        1_024,
        used_tokens=estimate_agent_messages_tokens(history),
        overhead_tokens=estimate_tokens(main.render_system_prompt()) + TOOL_SCHEMA_OVERHEAD_TOKENS,
        used_tokens_include_overhead=False,
    )
    assert cli_budget == expected
    assert cli_budget.over_limit is True


def test_budget_returns_none_when_window_unknown():
    """网关没报窗口时不猜——与 Web 的 `resolve_context_budget` 同一条放行路径。"""
    assert main._budget_for_cli("m", _history(100), context_window=None, max_output=None) is None
    assert main._budget_for_cli("m", _history(100), context_window=0, max_output=None) is None


def test_small_history_fits():
    budget = main._budget_for_cli("m", _history(10), context_window=128_000, max_output=4_096)

    assert budget is not None
    assert budget.over_limit is False


# ---------------------------------------------------------------------
# 单一事件循环
#
# Agent 来自 `agent_factory` 的 lru_cache，里面捧着 AsyncOpenAI→httpx 客户端。
# httpx 的内部原语绑在创建它们的 loop 上，跨 loop 复用会 RuntimeError。以前
# `build_cli_agent` 与 `run_interactive_loop` 各自 `asyncio.run`，能跑只是因为
# 装配阶段恰好没建连——加一次预热/探活就会在第一个 prompt 上炸。
# ---------------------------------------------------------------------
def test_cli_shares_one_event_loop(monkeypatch):
    loops: list[object] = []

    async def fake_build():
        loops.append(asyncio.get_running_loop())
        return object(), "model-key"

    async def fake_loop(_agent, _key):
        loops.append(asyncio.get_running_loop())

    async def fake_close():
        loops.append(asyncio.get_running_loop())

    async def fake_dispose():
        pass

    monkeypatch.setattr(main, "build_cli_agent", fake_build)
    monkeypatch.setattr(main, "run_interactive_loop", fake_loop)
    monkeypatch.setattr(main, "close_http_client", fake_close)
    monkeypatch.setattr(main, "engine", _FakeEngine(fake_dispose))

    asyncio.run(main._run_cli())

    assert len(loops) == 3
    assert len({id(loop) for loop in loops}) == 1, "装配、交互、关闭必须在同一个事件循环里"


def test_cli_releases_resources_even_when_the_loop_raises(monkeypatch):
    """交互循环抛异常时也要关闭客户端与数据库连接（否则 SQLite WAL 不折叠）。"""
    closed: list[str] = []

    async def fake_build():
        return object(), "model-key"

    async def exploding_loop(_agent, _key):
        raise RuntimeError("boom")

    async def fake_close():
        closed.append("http")

    async def fake_dispose():
        closed.append("db")

    monkeypatch.setattr(main, "build_cli_agent", fake_build)
    monkeypatch.setattr(main, "run_interactive_loop", exploding_loop)
    monkeypatch.setattr(main, "close_http_client", fake_close)
    monkeypatch.setattr(main, "engine", _FakeEngine(fake_dispose))

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(main._run_cli())

    assert closed == ["http", "db"]


def test_docs_hint_reflects_production(monkeypatch):
    """生产环境关掉了文档三件套，提示不能还说「/docs」。"""
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "enable_api_docs", None)
    assert "已关闭" in main.docs_hint()

    monkeypatch.setattr(settings, "app_env", "development")
    assert "/docs" in main.docs_hint()
