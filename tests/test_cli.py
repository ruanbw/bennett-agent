"""CLI 与 Web 的预算口径对齐（`main.py`）。

CLI 和 Web 共用同一份预算计算（`token_budget`）：同样的历史与窗口，`_budget_for_cli`
必须和 Web 的 `build_budget` 得出同样结论，否则会出现「本地跑得通、Web 却拒」——
而两边用的是同一个模型、同一份提示词。
"""

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

import main
from app.services.token_budget import (
    TOOL_SCHEMA_OVERHEAD_TOKENS,
    build_budget,
    estimate_agent_messages_tokens,
    estimate_tokens,
)


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


def test_parse_cli_command_recognises_builtin_commands():
    assert main.parse_cli_command("exit") == "exit"
    assert main.parse_cli_command("  QUIT ") == "exit"
    assert main.parse_cli_command("q") == "exit"
    assert main.parse_cli_command("clear") == "clear"
    assert main.parse_cli_command("Help") == "help"
    assert main.parse_cli_command("?") == "help"


def test_parse_cli_command_keeps_normal_prompts_as_prompts():
    """不能把「帮我把这个 clear 掉」之类的内容误当命令。"""
    assert main.parse_cli_command("帮我把这个 clear 掉") is None
    assert main.parse_cli_command("exit 之后怎么办") is None
    assert main.parse_cli_command("") is None


def test_cli_model_label_shows_resolved_route_when_env_is_ignored():
    assert main.format_cli_model_label("auto/model", "auto/model") == "auto/model"
    assert main.format_cli_model_label("", "group/model") == "group/model"
    assert main.format_cli_model_label("auto/model", "group/default") == ("auto/model（实际路由：group/default）")
    assert main.format_cli_model_label("", "") == "未配置"
