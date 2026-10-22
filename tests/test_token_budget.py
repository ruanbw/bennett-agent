"""上下文窗口预算的估算口径与判定契约。

这个模块的价值全在「估算偏高是安全方向、拿不到数据就放行」两条约定上，因此用例分成
四组：估算口径（含中文与全角的宽字符判定）、预留与安全系数怎么扣、三种必须**放行**
的未知情形、以及长输入要不要挪出事件循环。数字本身不是契约（换分词器就该变），
量级与方向才是。
"""

from dataclasses import FrozenInstanceError

import pytest

from app.config import settings
from app.schemas.chat import MAX_HISTORY_CHARS, MAX_MESSAGE_CHARS, ChatMessage
from app.services import token_budget
from app.services.token_budget import (
    ASCII_CHARS_PER_TOKEN,
    MESSAGE_OVERHEAD_TOKENS,
    THREAD_ESTIMATE_CHARS_THRESHOLD,
    TOOL_SCHEMA_OVERHEAD_TOKENS,
    ContextBudget,
    build_budget,
    estimate_agent_messages_tokens,
    estimate_agent_messages_tokens_async,
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


@pytest.fixture(autouse=True)
def _reset_once_warnings():
    """「只告警一次」的去重集合是模块级状态，用例之间必须隔离。"""
    token_budget.reset_token_budget_warnings()
    yield
    token_budget.reset_token_budget_warnings()


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


# 快路径要**锁机制**而不是锁耗时：墙钟阈值在覆盖率工具、共享 CI runner 上会偶发
# 变红，而且只能发现数量级退化。这里直接断言「一次逐字符判定都没发生」——删掉
# `isascii()` 分支时立刻失败，且与机器快慢无关。
def test_max_size_ascii_history_is_estimated_without_a_per_char_loop(monkeypatch):
    calls: list[str] = []
    original = token_budget._is_wide_char

    def counting(char: str) -> bool:
        calls.append(char)
        return original(char)

    monkeypatch.setattr(token_budget, "_is_wide_char", counting)

    text = "a" * MAX_HISTORY_CHARS
    tokens = estimate_tokens(text)
    # 先取计数再算参考值：`_reference_estimate_tokens` 自己也走 `_is_wide_char`
    # （它刻意逐字符复算），放在断言之后会把计数污染掉。
    per_char_calls = list(calls)

    assert tokens == MAX_HISTORY_CHARS // ASCII_CHARS_PER_TOKEN
    assert tokens == _reference_estimate_tokens(text)
    assert per_char_calls == [], "纯 ASCII 必须走 isascii() 快路径，不能退化成逐字符判定"


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
    # 真实 prompt 口径的上限：不再扣固定开销（真实 prompt 里本来就含它）
    assert budget.prompt_limit_tokens == int((100_000 - 20_000) * 0.9)

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
    # 两种口径的「真实 prompt 上限」相同：固定开销已经包含在真实 prompt 里
    assert included.prompt_limit_tokens == excluded.prompt_limit_tokens == int((100_000 - 8_000) * 0.9)


def test_prompt_limit_does_not_shrink_with_estimate_overhead():
    """用量事件里的上限是给下一轮用的：下一轮走锚点口径，不再扣固定开销。

    前端拿它当分母画「上下文占用」；如果这里错误地扣了 overhead，进度条会比后端
    实际判定更乐观/悲观（两种都不对，必须是同一个数）。
    """
    estimate = build_budget("m", 32_000, 8_000, 0, 5_000, used_tokens_include_overhead=False)

    assert estimate.limit_tokens == int((32_000 - 8_000 - 5_000) * 0.9)
    assert estimate.prompt_limit_tokens == int((32_000 - 8_000) * 0.9)


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
        prompt_limit_tokens=810_000,
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


def test_usable_anchor_rejects_a_token_count_below_the_structural_overhead(caplog):
    """锚点会顶替整份历史的估算，所以荒谬的小值不能放行。

    `{message_index: 5, context_tokens: 1}` 声称覆盖 6 条消息却只占 1 token，
    连这 6 条消息的结构性开销都装不下。
    """
    messages = [msg("user", "问"), msg("assistant", "答")] * 3

    with caplog.at_level("WARNING", logger="app.services.token_budget"):
        assert usable_anchor(messages, anchor(5, 1)) is None
    assert "index=5" in caplog.text and "context_tokens=1" in caplog.text


def test_usable_anchor_rejects_a_token_count_exactly_at_the_floor(caplog):
    """边界取严格大于：恰好等于结构性开销的锚点连自己的消息都装不下。"""
    messages = [msg("user", "问"), msg("assistant", "答")]
    floor = MESSAGE_OVERHEAD_TOKENS * 2

    with caplog.at_level("WARNING", logger="app.services.token_budget"):
        assert usable_anchor(messages, anchor(1, floor)) is None
    assert f"context_tokens={floor}" in caplog.text


def test_usable_anchor_accepts_a_token_count_just_above_the_floor():
    """略大过下限的锚点照常生效：下限只用来拦荒谬值，不是重新估算前缀。"""
    messages = [msg("user", "问"), msg("assistant", "答")]

    assert usable_anchor(messages, anchor(1, MESSAGE_OVERHEAD_TOKENS * 2 + 1)) is not None


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


# ---------------------------------------------------------------------
# 「网关没报窗口」的告警去重表必须有界
#
# key 来自 `model_name`，而网关列表不可用时任意字符串都会被放行（最长 300 字符）。
# 用 set 的话单个账号能把它持续灌大且永不清空——这是未认证输入驱动进程内存增长的
# 一条路径。
# ---------------------------------------------------------------------
def test_unknown_window_warning_table_is_bounded(monkeypatch):
    from app.services import token_budget

    monkeypatch.setattr(token_budget, "_WARNED_UNKNOWN_WINDOWS", {})
    limit = token_budget._WARNED_UNKNOWN_WINDOWS_MAX
    assert limit > 0

    for index in range(limit * 3):
        token_budget._warn_unknown_window_once(f"user-supplied-model-{index}")

    assert len(token_budget._WARNED_UNKNOWN_WINDOWS) == limit


def test_unknown_window_warning_is_deduplicated(monkeypatch):
    from app.services import token_budget

    monkeypatch.setattr(token_budget, "_WARNED_UNKNOWN_WINDOWS", {})

    token_budget._warn_unknown_window_once("m")
    token_budget._warn_unknown_window_once("m")

    assert list(token_budget._WARNED_UNKNOWN_WINDOWS) == ["m"]


# ---------------------------------------------------------------------
# 长文本估算挪出事件循环
#
# `resolve_context_budget` 是每个 /api/chat 请求的必经路径，而逐字符估算在 40 万字符
# 下是 300~500ms 的同步阻塞——单进程服务里这会把所有在飞的 SSE 一起卡住。这里锁的是
# 「大输入走线程、小输入不走」这个分流本身，不是耗时（墙钟阈值在共享 runner 上会抖）。
# ---------------------------------------------------------------------


def spy_on_to_thread(monkeypatch):
    """把 `asyncio.to_thread` 换成记录调用并立即同步执行的替身。

    替换的是 `token_budget.asyncio.to_thread`（模块对象与被测代码共享同一个 asyncio，
    所以等同替换全局入口），这样才真的证明走的是那个调度点。替身在协程里直接执行
    `func`，测试因此不需要额外的线程同步，结果也与同步路径可比。
    """
    calls = []

    async def fake_to_thread(func, *args, **kwargs):
        calls.append((func, args))
        return func(*args, **kwargs)

    monkeypatch.setattr(token_budget.asyncio, "to_thread", fake_to_thread)
    return calls


def history_with_chars(total_chars, pattern="中"):
    """造一段合计 `total_chars` 字符的合法历史。

    单条 `content` 上限是 `MAX_MESSAGE_CHARS`，所以大输入必须摊到多条消息上——
    这也正是真实场景：长历史是很多轮拼起来的，不是一条巨消息。
    """
    messages = []
    remaining = total_chars
    while remaining > 0:
        chunk = min(remaining, MAX_MESSAGE_CHARS)
        repeats = chunk // len(pattern) + 1
        messages.append(msg("user", (pattern * repeats)[:chunk]))
        remaining -= chunk
    return messages


async def test_large_history_is_estimated_in_a_thread(monkeypatch):
    """超过阈值的非 ASCII 历史必须走线程，且结果与同步估算逐位相同。"""
    patch_limits(monkeypatch, context_window=1_000_000, max_output_tokens=8_000)
    calls = spy_on_to_thread(monkeypatch)
    messages = history_with_chars(THREAD_ESTIMATE_CHARS_THRESHOLD + 1)

    budget = await resolve_context_budget("m", messages)

    assert budget is not None
    assert len(calls) == 1
    assert calls[0][0] is estimate_messages_tokens
    assert budget.used_tokens == estimate_messages_tokens(messages)


async def test_the_real_thread_path_produces_the_same_budget(monkeypatch):
    """不替换 `to_thread` 的一次真实执行：线程里跑同一个函数不会改变结果。

    上面的替身证明「分流点在哪」，这条证明「真的交给线程池也能正常返回」——
    pydantic 消息对象跨线程只读访问，不需要额外的序列化。
    """
    patch_limits(monkeypatch, context_window=10_000_000, max_output_tokens=8_000)
    messages = history_with_chars(THREAD_ESTIMATE_CHARS_THRESHOLD + 1)

    budget = await resolve_context_budget("m", messages)

    assert budget is not None
    assert budget.used_tokens == estimate_messages_tokens(messages)


async def test_small_history_is_estimated_without_a_thread(monkeypatch):
    """常规小请求不值得付一次线程调度：它们走同步快路径。"""
    patch_limits(monkeypatch, context_window=100_000, max_output_tokens=8_000)
    calls = spy_on_to_thread(monkeypatch)
    messages = [msg("user", "你好"), msg("assistant", "答")]

    budget = await resolve_context_budget("m", messages)

    assert budget is not None
    assert calls == []
    assert budget.used_tokens == estimate_messages_tokens(messages)


async def test_the_threshold_is_exclusive(monkeypatch):
    """恰好等于阈值时不走线程：判据是「超过」，边界写反会让边界大小的请求都白付一次线程池。"""
    patch_limits(monkeypatch, context_window=1_000_000, max_output_tokens=8_000)
    calls = spy_on_to_thread(monkeypatch)

    await resolve_context_budget("m", history_with_chars(THREAD_ESTIMATE_CHARS_THRESHOLD))
    assert calls == []

    await resolve_context_budget("m", history_with_chars(THREAD_ESTIMATE_CHARS_THRESHOLD + 1))
    assert len(calls) == 1


async def test_thinking_counts_toward_the_thread_threshold(monkeypatch):
    """阈值必须按 content + thinking 一起量：`thinking` 同样要逐字符估算。

    只看 `content` 时，一条「短文 + 超长思考链」的历史会走同步路径，而它恰恰是
    最慢的那种输入。
    """
    patch_limits(monkeypatch, context_window=1_000_000, max_output_tokens=8_000)
    calls = spy_on_to_thread(monkeypatch)
    messages = [msg("assistant", "答", thinking="思考" * (THREAD_ESTIMATE_CHARS_THRESHOLD // 2 + 1))]

    budget = await resolve_context_budget("m", messages)

    assert budget is not None
    assert len(calls) == 1
    assert budget.used_tokens == estimate_messages_tokens(messages)


def long_assistant_turn():
    """一条字符数超过阈值的 assistant 消息（正文 + 思考链一起算）。

    单条 `content` 上限 32k、`thinking` 64k，合起来才够越过 50k 的阈值——真实的
    长回答也确实大多长这样：正文与思考链并存。
    """
    return msg(
        "assistant",
        "答" * MAX_MESSAGE_CHARS,
        thinking="思" * (THREAD_ESTIMATE_CHARS_THRESHOLD - MAX_MESSAGE_CHARS + 1),
    )


async def test_the_anchor_slice_is_estimated_in_a_thread_when_long(monkeypatch):
    """锚点路径会先核对前缀、再估算切片；两段都长时都走线程。"""
    patch_limits(monkeypatch, context_window=1_000_000, max_output_tokens=8_000)
    calls = spy_on_to_thread(monkeypatch)
    messages = [*history_with_chars(10), long_assistant_turn(), msg("user", "新问题")]
    anchor_index = len(messages) - 2
    prefix_tokens = estimate_messages_tokens(messages[: anchor_index + 1])
    credible = prefix_tokens + 40_000

    budget = await resolve_context_budget("m", messages, anchor(anchor_index, credible))

    assert budget is not None and budget.basis == "anchor"
    # 一次前缀核对 + 一次切片估算
    assert len(calls) == 2
    assert budget.used_tokens == credible + estimate_messages_tokens(messages[anchor_index:])


async def test_anchor_verification_offloads_when_the_prefix_is_long(monkeypatch):
    """前缀核对本身按前缀长度分流：前缀很长时即使切片很短也要走线程。"""
    patch_limits(monkeypatch, context_window=1_000_000, max_output_tokens=8_000)
    calls = spy_on_to_thread(monkeypatch)
    messages = [*history_with_chars(THREAD_ESTIMATE_CHARS_THRESHOLD * 2), msg("assistant", "答"), msg("user", "新问题")]
    anchor_index = len(messages) - 2
    prefix_tokens = estimate_messages_tokens(messages[: anchor_index + 1])

    await resolve_context_budget("m", messages, anchor(anchor_index, prefix_tokens + 40_000))

    assert len(calls) == 1  # 前缀核对走线程，短切片走同步


@pytest.mark.parametrize(
    "pattern",
    ["中", "hello 中文 mixed text ", "a"],
    ids=["chinese", "mixed", "ascii"],
)
async def test_off_loop_and_synchronous_estimates_agree(monkeypatch, pattern):
    """分流不能改变结果：同一份历史必须得出同一个预算，与调度方式无关。"""
    patch_limits(monkeypatch, context_window=1_000_000, max_output_tokens=8_000)
    spy_on_to_thread(monkeypatch)
    messages = history_with_chars(THREAD_ESTIMATE_CHARS_THRESHOLD + 1, pattern=pattern)

    budget = await resolve_context_budget("m", messages)

    assert budget is not None
    assert budget.used_tokens == estimate_messages_tokens(messages)


async def test_agent_messages_async_wrapper_matches_the_sync_estimate(monkeypatch):
    """CLI 路径的异步包装与同步函数口径一致，且同样按量分流。"""
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    calls = spy_on_to_thread(monkeypatch)
    short = [ModelRequest(parts=[UserPromptPart(content="问题" * 100)])]
    # pydantic-ai 的部件没有长度上限，一条就能超过阈值
    long = [ModelRequest(parts=[UserPromptPart(content="你" * (THREAD_ESTIMATE_CHARS_THRESHOLD + 1))])]

    assert await estimate_agent_messages_tokens_async(short) == estimate_agent_messages_tokens(short)
    assert calls == []

    assert await estimate_agent_messages_tokens_async(long) == estimate_agent_messages_tokens(long)
    assert len(calls) == 1
    assert calls[0][0] is estimate_agent_messages_tokens


# ---------------------------------------------------------------------
# 已解析的快照优先：对话路径不该为预算再查一遍库
# ---------------------------------------------------------------------
async def test_pre_resolved_limits_skip_the_lookup(monkeypatch):
    """传了 `model_limits` 就不该再按名字查库：对话路径已经有一份快照。

    多查一次不只是慢：两次读之间管理员改了配置，会出现「预算按旧窗口算、实际调用走
    新窗口」。
    """

    async def boom(_model_id):
        raise AssertionError("不该再按名字查一次模型窗口")

    monkeypatch.setattr(token_budget, "get_model_limits", boom)

    budget = await resolve_context_budget(
        "m",
        [msg("user", "abcd")],
        model_limits=(100_000, 8_192),
    )

    assert budget is not None
    assert budget.context_window == 100_000
    assert budget.max_output_tokens == 8_192


async def test_pre_resolved_unknown_window_still_allows_through(monkeypatch):
    """快照里没有窗口时与「网关没报」同一条放行路径，不能因为传了参数就改判。"""
    patch_limits(monkeypatch, context_window=100_000)

    assert await resolve_context_budget("m", [msg("user", "你好")], model_limits=(None, None)) is None


async def test_gateway_path_without_snapshot_still_looks_limits_up(monkeypatch):
    """不传 `model_limits` 的调用方（CLI / 直接调用）保持原行为。"""
    patch_limits(monkeypatch, context_window=64_000, max_output_tokens=4_096)

    budget = await resolve_context_budget("m", [msg("user", "abcd")])

    assert budget is not None
    assert budget.context_window == 64_000


# ---------------------------------------------------------------------
# 坏锚点只告警一次：客户端版本错位时会随每一条消息重复出现
# ---------------------------------------------------------------------
def test_bad_anchor_reason_is_warned_only_once(caplog):
    messages = [msg("user", "问"), msg("assistant", "答")]

    with caplog.at_level("WARNING", logger="app.services.token_budget"):
        for _ in range(3):
            assert usable_anchor(messages, anchor(99, 5_000)) is None

    assert len([record for record in caplog.records if "越界" in record.getMessage()]) == 1


def test_different_anchor_problems_are_each_warned(caplog):
    messages = [msg("user", "问"), msg("assistant", "答")]

    with caplog.at_level("WARNING", logger="app.services.token_budget"):
        usable_anchor(messages, anchor(99, 5_000))  # 越界
        usable_anchor([msg("user", "问")], anchor(0, 5_000))  # 指向 user 消息
        usable_anchor(messages, anchor(1, 8))  # 恰好等于结构性开销下限

    messages_text = [record.getMessage() for record in caplog.records]
    assert any("越界" in text for text in messages_text)
    assert any("不是 assistant" in text for text in messages_text)
    assert any("结构性开销" in text for text in messages_text)


def test_reset_allows_the_warning_again(caplog):
    messages = [msg("user", "问"), msg("assistant", "答")]
    with caplog.at_level("WARNING", logger="app.services.token_budget"):
        usable_anchor(messages, anchor(99, 5_000))
        token_budget.reset_token_budget_warnings()
        usable_anchor(messages, anchor(99, 5_000))

    assert len([record for record in caplog.records if "越界" in record.getMessage()]) == 2


async def test_understated_anchor_is_rejected(monkeypatch):
    """客户端报一个「够过结构下限、但远小于前缀」的 token 数，不能借此跳过预检（I-08）。"""
    patch_limits(monkeypatch, context_window=100_000, max_output_tokens=8_000)
    messages = [msg("user", "很长的前缀" * 2_000), msg("assistant", "很长的回答" * 2_000), msg("user", "新问题")]

    # 结构下限只按消息条数算，这个值能过 usable_anchor，但远小于前缀真实估算
    floor = MESSAGE_OVERHEAD_TOKENS * 2 + 1
    budget = await resolve_context_budget("m", messages, anchor(1, floor))

    assert budget is not None
    assert budget.basis == "estimate"
    assert budget.used_tokens == estimate_messages_tokens(messages)


async def test_credible_anchor_is_still_used(monkeypatch):
    patch_limits(monkeypatch, context_window=100_000, max_output_tokens=8_000)
    messages = [msg("user", "问"), msg("assistant", "答"), msg("user", "新问题")]

    budget = await resolve_context_budget("m", messages, anchor(1, 40_000))

    assert budget is not None and budget.basis == "anchor"
