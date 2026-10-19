"""上下文窗口预算的估算口径与判定契约。

这个模块的价值全在「估算偏高是安全方向、拿不到数据就放行」两条约定上，因此用例分成
三组：估算口径（含中文与全角的宽字符判定）、预留与安全系数怎么扣、以及三种必须
**放行**的未知情形。数字本身不是契约（换分词器就该变），量级与方向才是。
"""

import time
from dataclasses import FrozenInstanceError

import pytest

from app.config import settings
from app.schemas.chat import MAX_HISTORY_CHARS, ChatMessage
from app.services import token_budget
from app.services.token_budget import (
    ASCII_CHARS_PER_TOKEN,
    MESSAGE_OVERHEAD_TOKENS,
    TOOL_SCHEMA_OVERHEAD_TOKENS,
    ContextBudget,
    build_budget,
    estimate_messages_tokens,
    estimate_tokens,
    overflow_message,
    resolve_context_budget,
    resolve_reserve_tokens,
    usable_anchor,
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


def _reference_estimate_tokens(text: str) -> int:
    """逐字符参考实现：宽窄分开累计后再向上取整，不走被测函数的循环与 ASCII 快路径。

    宽字符判定用生产谓词 `_is_wide_char`（它本身由 `test_estimate_tokens_is_language_aware`
    逐语言锁定），这里独立复算的是「宽窄分开累计」与「窄字符按 4 向上取整」。
    """
    wide = sum(1 for char in text if token_budget._is_wide_char(char))
    narrow = len(text) - wide
    return wide + -(-narrow // ASCII_CHARS_PER_TOKEN)


# 本机（CPython 3.10）实测基线，不是阈值：400k 纯 ASCII 约 0.01ms（isascii 快路径），
# 400k 中文约 330ms、中英混排约 520ms（逐字符循环）。非 ASCII 只锁结果、不锁时间。
def test_max_size_ascii_history_is_estimated_without_a_per_char_loop():
    text = "a" * MAX_HISTORY_CHARS

    start = time.perf_counter()
    tokens = estimate_tokens(text)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert tokens == MAX_HISTORY_CHARS // ASCII_CHARS_PER_TOKEN
    assert tokens == _reference_estimate_tokens(text)
    assert elapsed_ms < 50, f"{MAX_HISTORY_CHARS} 字符 ASCII 估算耗时 {elapsed_ms:.1f}ms，快路径失效了"


@pytest.mark.parametrize(
    "text",
    [
        ("中文分词与全角标点，测。" * 40_000)[:MAX_HISTORY_CHARS],
        ("hello world 这是一段中文 test " * 20_000)[:MAX_HISTORY_CHARS],
    ],
    ids=["chinese", "mixed"],
)
def test_max_size_non_ascii_history_matches_the_reference_count(text):
    """非 ASCII 路径可以慢，但不能算错：宽窄混排的结果必须与逐字符口径逐字一致。"""
    assert estimate_tokens(text) == _reference_estimate_tokens(text)


def test_wide_and_narrow_runs_are_summed_separately():
    """宽窄分开累计：合并成「总长度 / 4」会把中文段低估成四分之一。"""
    text = "中文" * 100_000 + "x" * 200_001

    assert estimate_tokens(text) == 200_000 + -(-200_001 // ASCII_CHARS_PER_TOKEN)
    assert estimate_tokens(text) != -(-len(text) // ASCII_CHARS_PER_TOKEN)


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
        used_tokens_include_overhead=False,
    )

    assert budget.reserve_tokens == 20_000
    assert budget.limit_tokens == int((100_000 - 20_000 - 1_000) * 0.9)

    at_limit = build_budget(
        model="m",
        context_window=100_000,
        max_output_tokens=20_000,
        used_tokens=budget.limit_tokens,
        overhead_tokens=1_000,
        used_tokens_include_overhead=False,
    )
    # 恰好等于上限不拒绝：判据是「超过」而不是「达到」
    assert not at_limit.over_limit and at_limit.remaining_tokens == 0


def test_overhead_is_only_deducted_when_used_excludes_it():
    """`used_tokens` 已含固定开销时不能再从上限里扣一次，否则同一笔开销被扣两次。"""
    excluded = build_budget("m", 100_000, 8_000, 10_000, 1_000, used_tokens_include_overhead=False)
    included = build_budget("m", 100_000, 8_000, 10_000, 1_000, used_tokens_include_overhead=True)

    assert included.limit_tokens == int((100_000 - 8_000) * 0.9)
    assert excluded.limit_tokens == int((100_000 - 8_000 - 1_000) * 0.9)


def test_safety_ratio_is_configurable(monkeypatch):
    monkeypatch.setattr(settings, "context_safety_ratio", 0.5)
    monkeypatch.setattr(settings, "context_reserve_tokens", 0)
    budget = build_budget("m", 100_000, 0, 0, 0, used_tokens_include_overhead=False)

    assert budget.limit_tokens == 50_000


def test_limit_never_goes_negative():
    """窗口比「预留 + 固定开销」还小：上限取 0 而不是负数。

    负数会让 `remaining_tokens` 的语义反转（超限判定依旧成立，但所有基于
    「还剩多少」的裁剪规划都会算错）。
    """
    budget = build_budget("m", 1_000, 0, 0, overhead_tokens=999_999, used_tokens_include_overhead=False)

    assert budget.limit_tokens == 0


def test_over_limit_and_remaining_tokens():
    over = build_budget("m", 100_000, 0, used_tokens=100_000, overhead_tokens=0, used_tokens_include_overhead=False)
    under = build_budget("m", 100_000, 0, used_tokens=10, overhead_tokens=0, used_tokens_include_overhead=False)

    assert over.over_limit and over.remaining_tokens < 0
    assert not under.over_limit and under.remaining_tokens > 0


def test_exactly_at_the_limit_is_allowed_but_one_token_over_is_not():
    """边界锁死：判据是「超过」而不是「达到」。

    `used == limit` 一旦被判成超限，每个贴着上限的请求都会被弹回，前端还会跟着锁住
    输入框；而 `>` 被写成 `>=` 的回归只会正好在这一格露头，所以两边都要断言。
    """
    params = {
        "model": "m",
        "context_window": 100_000,
        "max_output_tokens": 20_000,
        "overhead_tokens": 1_000,
        "used_tokens_include_overhead": False,
    }
    limit_tokens = build_budget(used_tokens=0, **params).limit_tokens
    assert limit_tokens > 0

    at_limit = build_budget(used_tokens=limit_tokens, **params)
    one_over = build_budget(used_tokens=limit_tokens + 1, **params)

    assert at_limit.over_limit is False
    assert one_over.over_limit is True


def test_budget_is_frozen_and_compares_by_value():
    """预算对象是纯数据：两个消费方（拒绝判定、裁剪规划）必须看到同一份数字。"""
    budget = build_budget("m", 100_000, 0, 0, 0, used_tokens_include_overhead=False)
    assert budget == build_budget("m", 100_000, 0, 0, 0, used_tokens_include_overhead=False)

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


# ---------------------------------------------------------------------
# 锚点：沿用上一轮的真实用量，只估算新增消息
# ---------------------------------------------------------------------
def anchor(index, tokens):
    from app.schemas.chat import ContextAnchor

    return ContextAnchor(message_index=index, context_tokens=tokens)


def test_usable_anchor_needs_an_assistant_message(caplog):
    """用量是那一轮回答产生的：指向 user 消息说明客户端算错了位置。"""
    messages = [msg("user", "问"), msg("assistant", "答"), msg("user", "再问")]

    assert usable_anchor(messages, anchor(1, 5_000)) is not None
    with caplog.at_level("WARNING", logger="app.services.token_budget"):
        assert usable_anchor(messages, anchor(0, 5_000)) is None
    assert "不是 assistant" in caplog.text


@pytest.mark.parametrize("index", [2, 99])
def test_usable_anchor_rejects_out_of_range(index, caplog):
    """越界就退回估算，不能让一个坏下标打断整轮对话。"""
    messages = [msg("user", "问"), msg("assistant", "答")]

    with caplog.at_level("WARNING", logger="app.services.token_budget"):
        assert usable_anchor(messages, anchor(index, 5_000)) is None
    assert "越界" in caplog.text


def test_usable_anchor_rejects_a_negative_index_built_bypassing_the_schema(caplog):
    """负下标由 schema 拦（ge=0），但本函数是公开的，也要自己保一道。"""
    from app.schemas.chat import ContextAnchor

    negative = ContextAnchor.model_construct(message_index=-1, context_tokens=5_000)
    messages = [msg("user", "问"), msg("assistant", "答")]

    with caplog.at_level("WARNING", logger="app.services.token_budget"):
        assert usable_anchor(messages, negative) is None
    assert "越界" in caplog.text


def test_usable_anchor_passes_through_when_absent():
    assert usable_anchor([msg("user", "问")], None) is None


async def test_anchor_counts_the_anchor_reply_and_the_messages_after_it(monkeypatch):
    """锚点值是产生那条助手消息的**那次请求**的 prompt，不含它自己。

    估算若从 `message_index + 1` 起，锚点那轮回答既不在真实值里也没被估算，
    一条长回答就会让预算少算一整轮输出。
    """
    patch_limits(monkeypatch, context_window=100_000, max_output_tokens=8_000)
    messages = [msg("user", "很长的历史" * 100), msg("assistant", "很长的回答" * 100), msg("user", "新问题")]

    blind = await resolve_context_budget("m", messages)
    anchored = await resolve_context_budget("m", messages, anchor(1, 40_000))

    assert blind is not None and anchored is not None
    assert blind.basis == "estimate"
    assert anchored.basis == "anchor"
    # 锚点值 + 锚点那条回答 + 之后的消息
    assert anchored.used_tokens == 40_000 + estimate_messages_tokens(messages[1:])
    # 盲估把整份历史都估算了一遍
    assert blind.used_tokens == estimate_messages_tokens(messages)
    # 锚点值已含固定开销，上限不再扣；盲估只估算 messages，上限要扣
    assert anchored.limit_tokens == int((100_000 - 8_000) * 0.9)
    assert blind.limit_tokens == int((100_000 - 8_000 - blind.overhead_tokens) * 0.9)


async def test_a_longer_anchor_reply_costs_more_budget(monkeypatch):
    """把锚点那条回答加长，用量必须跟着变大——它真的占着窗口。"""
    patch_limits(monkeypatch, context_window=100_000, max_output_tokens=8_000)
    short = [msg("user", "问"), msg("assistant", "答"), msg("user", "新问题")]
    longer = [msg("user", "问"), msg("assistant", "答" * 1_000), msg("user", "新问题")]

    short_budget = await resolve_context_budget("m", short, anchor(1, 40_000))
    longer_budget = await resolve_context_budget("m", longer, anchor(1, 40_000))

    assert short_budget is not None and longer_budget is not None
    assert short_budget.basis == longer_budget.basis == "anchor"
    assert longer_budget.used_tokens - short_budget.used_tokens == estimate_tokens("答" * 1_000) - estimate_tokens("答")


async def test_unusable_anchor_falls_back_to_the_blind_estimate(monkeypatch):
    patch_limits(monkeypatch, context_window=100_000, max_output_tokens=8_000)
    messages = [msg("user", "问"), msg("assistant", "答"), msg("user", "再问")]

    budget = await resolve_context_budget("m", messages, anchor(0, 40_000))

    assert budget is not None
    assert budget.basis == "estimate"
    assert budget.used_tokens == estimate_messages_tokens(messages)
