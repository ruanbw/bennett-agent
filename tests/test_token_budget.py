"""上下文窗口预算的估算口径与判定契约。

这个模块的价值全在「估算偏高是安全方向、拿不到数据就放行」两条约定上，因此用例分成
三组：估算口径（含中文与全角的宽字符判定）、预留与安全系数怎么扣、以及三种必须
**放行**的未知情形。数字本身不是契约（换分词器就该变），量级与方向才是。
"""

from dataclasses import FrozenInstanceError

import pytest

from app.config import settings
from app.schemas.chat import ChatMessage
from app.services import token_budget
from app.services.token_budget import (
    MESSAGE_OVERHEAD_TOKENS,
    TOOL_SCHEMA_OVERHEAD_TOKENS,
    ContextBudget,
    build_budget,
    estimate_messages_tokens,
    estimate_tokens,
    overflow_message,
    resolve_context_budget,
    resolve_reserve_tokens,
)


@pytest.fixture(autouse=True)
def _isolate_module_state(monkeypatch):
    """隔离模块级告警去重集合与预算相关配置。

    `_WARNED_UNKNOWN_WINDOWS` 是全局的（按模型 id 去重），不隔离就会「单跑通过、
    全量失败」；配置项用默认值恢复，避免某个用例改了安全系数影响后面的用例。
    """
    token_budget._WARNED_UNKNOWN_WINDOWS.clear()
    monkeypatch.setattr(settings, "context_guard_mode", "reject")
    monkeypatch.setattr(settings, "context_reserve_tokens", 8192)
    monkeypatch.setattr(settings, "context_safety_ratio", 0.9)
    yield
    token_budget._WARNED_UNKNOWN_WINDOWS.clear()


def msg(role="user", content="", thinking=None):
    return ChatMessage(role=role, content=content, thinking=thinking)


def patch_limits(monkeypatch, context_window, max_output_tokens=None):
    """把 `get_model_limits` 换成固定返回值。

    用 `setattr` 打在 `token_budget` 上而不是 `model_service` 上：本模块按名字导入，
    改源模块对已绑定的名字无效——这正是「测试通过但线上没生效」的经典形态。
    """

    async def fake_limits(_model_id):
        return context_window, max_output_tokens

    monkeypatch.setattr(token_budget, "get_model_limits", fake_limits)


# ---------------------------------------------------------------------
# 估算口径
# ---------------------------------------------------------------------
def test_empty_text_costs_nothing():
    assert estimate_tokens("") == 0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("abcd", 1),  # 正好一个 token 宽度的英文
        ("abcde", 2),  # 向上取整，宁可多算
        ("a" * 40, 10),
        ("你好", 2),  # 汉字按 1 token/字
        ("你好，世界！", 6),  # 全角标点同样按宽字符算
        ("こんにちは", 5),  # 假名
        ("안녕하세요", 5),  # 谚文音节
        ("Привет", 2),  # 非 ASCII 但非宽字符：仍按 4 字符/token，不能翻四倍
        ("hi你好", 3),  # 1 + 2
    ],
)
def test_estimate_tokens_is_language_aware(text, expected):
    assert estimate_tokens(text) == expected


def test_cyrillic_is_not_treated_as_wide():
    """Ambiguous 宽度的文字不能被算成宽字符。

    用 `east_asian_width` 判定会命中此例：西里尔字母是 Ambiguous，一段俄文用量会
    被算成四倍，进而把一堆正常请求误判成超限。
    """
    russian = "Привет мир, как дела сегодня"
    assert estimate_tokens(russian) == -(-len(russian) // 4)


def test_estimate_messages_counts_thinking_and_per_message_overhead():
    messages = [msg("user", "abcd"), msg("assistant", "abcd", thinking="你好")]

    # 两条消息的结构性开销 + 1 + 1 + 2
    assert estimate_messages_tokens(messages) == 2 * MESSAGE_OVERHEAD_TOKENS + 4
    assert estimate_messages_tokens([]) == 0


# ---------------------------------------------------------------------
# 预留与上限
# ---------------------------------------------------------------------
def test_declared_max_output_tokens_wins_over_the_configured_default(monkeypatch):
    monkeypatch.setattr(settings, "context_reserve_tokens", 4096)
    assert resolve_reserve_tokens(100_000, 32_000) == 32_000


@pytest.mark.parametrize("declared", [None, 0, -1, True, "32000"])
def test_unusable_max_output_tokens_falls_back_to_the_configured_reserve(monkeypatch, declared):
    """上游声明的字段不可用（缺失、非正、布尔、字符串）时用配置值。

    不接受字符串是有意的：`"32000"` 与 `32000` 在这里都不是「上游没说」，
    但把它 `int()` 出来等于替上游伪造一个数字，与 `_parse_int_field` 的取舍一致。
    """
    monkeypatch.setattr(settings, "context_reserve_tokens", 4096)
    assert resolve_reserve_tokens(100_000, declared) == 4096


def test_reserve_never_eats_more_than_half_the_window():
    """网关把最大输出报成与窗口同量级时，扣完不能把可用历史压成 0。"""
    assert resolve_reserve_tokens(32_000, 32_000) == 16_000
    assert resolve_reserve_tokens(1000, 999_999) == 500


def test_limit_applies_the_safety_ratio():
    budget = build_budget(
        model="m",
        context_window=100_000,
        max_output_tokens=20_000,
        used_tokens=0,
        overhead_tokens=1_000,
    )

    assert budget.reserve_tokens == 20_000
    assert budget.limit_tokens == int((100_000 - 20_000 - 1_000) * 0.9)


def test_safety_ratio_is_configurable(monkeypatch):
    monkeypatch.setattr(settings, "context_safety_ratio", 0.5)
    monkeypatch.setattr(settings, "context_reserve_tokens", 0)
    budget = build_budget("m", 100_000, 0, 0, 0)

    assert budget.limit_tokens == 50_000


def test_limit_never_goes_negative():
    """窗口比「预留 + 固定开销」还小：上限取 0 而不是负数。

    负数会让 `remaining_tokens` 的语义反转（超限判定依旧成立，但所有基于
    「还剩多少」的裁剪规划都会算错）。
    """
    budget = build_budget("m", 1_000, 0, 0, overhead_tokens=999_999)

    assert budget.limit_tokens == 0


def test_over_limit_and_remaining_tokens():
    over = build_budget("m", 100_000, 0, used_tokens=100_000, overhead_tokens=0)
    under = build_budget("m", 100_000, 0, used_tokens=10, overhead_tokens=0)

    assert over.over_limit and over.remaining_tokens < 0
    assert not under.over_limit and under.remaining_tokens > 0


def test_budget_is_frozen_and_compares_by_value():
    """预算对象是纯数据：两个消费方（拒绝判定、裁剪规划）必须看到同一份数字。"""
    budget = build_budget("m", 100_000, 0, 0, 0)
    assert budget == build_budget("m", 100_000, 0, 0, 0)

    with pytest.raises(FrozenInstanceError):
        budget.limit_tokens = 1  # type: ignore[misc]


# ---------------------------------------------------------------------
# 解析预算：三种必须放行的情形
# ---------------------------------------------------------------------
async def test_guard_off_skips_the_check_entirely(monkeypatch):
    monkeypatch.setattr(settings, "context_guard_mode", "off")
    patch_limits(monkeypatch, context_window=None)

    assert await resolve_context_budget("m", [msg("user", "你好")]) is None


async def test_unknown_window_from_the_gateway_is_allowed_through(monkeypatch, caplog):
    """网关没报窗口就放行，并留一条（只留一条）WARNING。

    与 `get_known_model_ids` 的取舍一致：拿不到数据时不能拿编出来的窗口拦用户。
    """
    patch_limits(monkeypatch, context_window=None)

    with caplog.at_level("WARNING", logger="app.services.token_budget"):
        assert await resolve_context_budget("m", [msg("user", "你好")]) is None
        assert await resolve_context_budget("m", [msg("user", "你好")]) is None

    assert len(caplog.records) == 1
    assert "m" in caplog.records[0].getMessage()


@pytest.mark.parametrize("window", [0, -1])
async def test_non_positive_window_is_treated_as_unknown(monkeypatch, window):
    patch_limits(monkeypatch, context_window=window)
    assert await resolve_context_budget("m", [msg("user", "你好")]) is None


async def test_model_missing_from_the_gateway_list_is_allowed_through(monkeypatch):
    patch_limits(monkeypatch, context_window=None, max_output_tokens=None)
    assert await resolve_context_budget("不在列表里的模型", [msg("user", "你好")]) is None


async def test_resolved_budget_counts_system_prompt_and_tools(monkeypatch):
    patch_limits(monkeypatch, context_window=100_000, max_output_tokens=8_192)

    budget = await resolve_context_budget("m", [msg("user", "abcd")])

    assert budget is not None
    assert budget.model == "m"
    assert budget.context_window == 100_000
    # 固定开销 = 系统提示词 + 工具 schema 常量，必须被扣掉
    assert budget.overhead_tokens > TOOL_SCHEMA_OVERHEAD_TOKENS
    assert budget.used_tokens == MESSAGE_OVERHEAD_TOKENS + 1


async def test_resolved_budget_flags_an_over_limit_history(monkeypatch):
    patch_limits(monkeypatch, context_window=8_192, max_output_tokens=0)
    monkeypatch.setattr(settings, "context_reserve_tokens", 1_000)

    budget = await resolve_context_budget("m", [msg("user", "你" * 9_000)])

    assert budget is not None and budget.over_limit


# ---------------------------------------------------------------------
# 文案
# ---------------------------------------------------------------------
def test_overflow_message_carries_numbers_and_actions():
    budget = ContextBudget(
        model="group/auto-x",
        context_window=1_000_000,
        max_output_tokens=100_000,
        reserve_tokens=100_000,
        overhead_tokens=1_000,
        limit_tokens=810_000,
        used_tokens=900_000,
    )

    message = overflow_message(budget)

    assert "900,000" in message and "810,000" in message and "1,000,000" in message
    assert "group/auto-x" in message
    # 两个动作缺一不可：只告诉用户「超了」等于让他自己猜怎么继续
    assert "新建会话" in message and "模型" in message
