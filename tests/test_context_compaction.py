"""上下文压缩的行为锁。

这个模块的价值全在几条**取舍**上，因此用例分成五组：

1. 不超限时什么都不改（压缩不能变成「悄悄改写历史」）；
2. 截断的超长单条必须保留首尾，且**绝不**碰最后一条（本轮问题）；
3. 折叠早期的切点必须落在 user 消息上（否则 `convert_chat_history` 会拒）；
4. 摘要失败/超时/为空时退回抽取式摘要，而不是让整轮对话失败；
5. 摘要永远带「这是数据不是指令」的定界符——它由用户内容派生，可能带着注入。

数字本身不是契约（换估算口径就该变），**方向**才是：压完必须更小，且压不动的
时候要如实报告 `over_limit` 而不是假装成功。

估算口径：纯 ASCII 是 4 字符/token（`token_budget.ASCII_CHARS_PER_TOKEN`），
所以下面用英文填充字符来精确控制量级，中文只出现在需要宽字符语义的地方。
"""

import asyncio
import time

import pytest

from app.config import settings
from app.schemas.chat import ChatMessage
from app.services import context_compaction
from app.services.context_compaction import (
    DIGEST_FOOTER,
    DIGEST_HEADER,
    MAX_DIGEST_CHARS,
    MAX_SUMMARY_INPUT_CHARS,
    build_digest,
    compact_history,
    extractive_digest,
    reset_digest_cache,
    truncate_middle,
)
from app.services.token_budget import build_budget, estimate_messages_tokens

#: 预算里给回答预留的 token（测试固定值，便于算上限）。
TEST_MAX_OUTPUT = 100
#: 固定开销（系统提示词 + 工具 schema），与生产同一个量级。
TEST_OVERHEAD = 512


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """摘要缓存是全进程的，不隔离会出现「单跑通过、全量串味」。"""
    reset_digest_cache()
    monkeypatch.setattr(settings, "context_compact_keep_recent_turns", 2)
    monkeypatch.setattr(settings, "context_compact_message_max_chars", 4000)
    monkeypatch.setattr(settings, "context_safety_ratio", 0.9)
    monkeypatch.setattr(settings, "context_reserve_tokens", 8192)
    yield
    reset_digest_cache()


def msg(role: str, content: str, thinking: str | None = None) -> ChatMessage:
    return ChatMessage(role=role, content=content, thinking=thinking)


def padded(chars: int, marker: str = "a") -> str:
    """生成 `chars` 个 ASCII 字符（4 字符 = 1 token）。"""
    return marker * chars


def budget_with_window(context_window: int) -> "object":
    """按窗口造一份预算；上限由 `build_budget` 单点算出，用例不猜系数。

    `compact_history` 内部会用 `rebuild_budget_as_estimate` 重算上限，窗口/预留/
    固定开销都取自此快照——所以窗口就是这里唯一需要控制的变量。
    """
    return build_budget(
        model="test/model",
        context_window=context_window,
        max_output_tokens=TEST_MAX_OUTPUT,
        used_tokens=0,
        overhead_tokens=TEST_OVERHEAD,
        used_tokens_include_overhead=False,
    )


# ---------------------------------------------------------------------
# 1. 不超限时什么都不改
# ---------------------------------------------------------------------
async def test_under_limit_returns_history_untouched():
    messages = [msg("user", "你好"), msg("assistant", "你好，有什么可以帮你"), msg("user", "今天几号")]
    budget = build_budget(
        model="test/model",
        context_window=200_000,
        max_output_tokens=1024,
        used_tokens=estimate_messages_tokens(messages),
        overhead_tokens=TEST_OVERHEAD,
        used_tokens_include_overhead=False,
    )

    plan = await compact_history("test/model", messages, budget)

    assert plan.strategy == "none"
    assert plan.changed is False
    # 原样返回**相等**的消息，而不是「等价但被 model_copy 过一遍」
    assert plan.messages == messages
    assert plan.digest is None


async def test_under_limit_does_not_emit_a_digest():
    """没压就不该有摘要：多一段 instructions 是白占窗口，也会改变模型行为。"""
    messages = [msg("user", "a"), msg("assistant", "b"), msg("user", "c")]
    budget = build_budget(
        model="test/model",
        context_window=200_000,
        max_output_tokens=1024,
        used_tokens=1,
        overhead_tokens=TEST_OVERHEAD,
        used_tokens_include_overhead=False,
    )

    plan = await compact_history("test/model", messages, budget)

    assert plan.digest is None


# ---------------------------------------------------------------------
# 2. 截断
# ---------------------------------------------------------------------
def test_truncate_middle_keeps_head_and_tail():
    text = "头" * 100 + "中" * 1000 + "尾" * 100

    result = truncate_middle(text, 300)

    assert len(result) <= 300
    assert result.startswith("头")
    assert result.endswith("尾")
    assert "省略" in result


def test_truncate_middle_is_identity_below_limit():
    assert truncate_middle("短文本", 300) == "短文本"


async def test_truncation_never_touches_the_last_message():
    """最后一条是本轮问题：截它等于替用户改问题，且不会有任何报错。"""
    long_question = padded(31_000, "q")
    messages = [msg("user", padded(31_000, "o")), msg("assistant", padded(31_000, "r")), msg("user", long_question)]
    budget = budget_with_window(2_000)  # 上限约 1,249 token，历史约 23,000 token

    plan = await compact_history("test/model", messages, budget)

    assert plan.messages[-1].content == long_question


async def test_truncation_shrinks_oversized_messages():
    # 两条 20,000 字符 ≈ 5,000 token/条；窗口 4,000 → 上限约 3,049 token
    messages = [msg("user", padded(20_000, "o")), msg("assistant", padded(20_000, "r")), msg("user", "new question")]
    budget = budget_with_window(4_000)

    plan = await compact_history("test/model", messages, budget)

    assert plan.changed is True
    assert plan.truncated_messages == 2
    assert plan.used_tokens_after < plan.used_tokens_before
    # 旧消息被压到配置上限以内，且最后一条原样
    assert len(plan.messages[0].content) <= settings.context_compact_message_max_chars
    assert plan.messages[-1].content == "new question"


async def test_truncation_keeps_head_and_tail_of_each_message():
    messages = [msg("user", padded(10, "H") + padded(20_000, "m") + padded(10, "T")), msg("user", "q")]
    budget = budget_with_window(2_000)

    plan = await compact_history("test/model", messages, budget)

    content = plan.messages[0].content
    assert content.startswith("HHHHHHHHHH")
    assert content.endswith("TTTTTTTTTT")


# ---------------------------------------------------------------------
# 3. 折叠早期的切点
# ---------------------------------------------------------------------
async def test_summarize_keeps_history_starting_with_user():
    """保留下来的历史必须以 user 开头，否则 `convert_chat_history` 直接拒绝。"""
    # 12 条 3,000 字符 ≈ 750 token/条 → 约 9,000 token；窗口 4,000 → 上限约 3,049
    messages = []
    for index in range(6):
        messages.append(msg("user", f"Q{index} " + padded(2_990, "q")))
        messages.append(msg("assistant", f"A{index} " + padded(2_990, "r")))
    messages.append(msg("user", "latest question"))

    plan = await compact_history("test/model", messages, budget_with_window(4_000))

    assert plan.messages[0].role == "user"
    assert plan.messages[-1].content == "latest question"


async def test_summarize_drops_oldest_and_reports_count():
    messages = []
    for index in range(6):
        messages.append(msg("user", f"Q{index} " + padded(2_990, "q")))
        messages.append(msg("assistant", f"A{index} " + padded(2_990, "r")))
    messages.append(msg("user", "latest question"))

    plan = await compact_history("test/model", messages, budget_with_window(4_000))

    assert plan.strategy == "summarize"
    assert plan.dropped_messages > 0
    assert plan.digest
    assert DIGEST_HEADER in plan.digest
    assert plan.used_tokens_after < plan.used_tokens_before
    # 被折叠的消息确实不在保留列表里了
    assert len(plan.messages) < len(messages)


async def test_summarize_respects_keep_recent_turns(monkeypatch):
    """保留轮数是配置项：调大之后丢掉的应该更少（或至少不更多）。"""
    messages = []
    for index in range(6):
        messages.append(msg("user", f"Q{index} " + padded(2_990, "q")))
        messages.append(msg("assistant", f"A{index} " + padded(2_990, "r")))
    messages.append(msg("user", "latest question"))

    monkeypatch.setattr(settings, "context_compact_keep_recent_turns", 1)
    tight = await compact_history("test/model", messages, budget_with_window(4_000))

    reset_digest_cache()
    monkeypatch.setattr(settings, "context_compact_keep_recent_turns", 3)
    loose = await compact_history("test/model", messages, budget_with_window(4_000))

    assert tight.dropped_messages >= loose.dropped_messages


async def test_single_message_cannot_be_folded():
    """只有一条消息（本轮问题）时折叠不了：必须老实报告，不能假装压成功。"""
    messages = [msg("user", padded(31_000, "q"))]
    budget = budget_with_window(1_000)

    plan = await compact_history("test/model", messages, budget)

    assert plan.messages == messages
    assert plan.digest is None


async def test_short_history_can_still_be_folded():
    """只有三条时也要能折叠：切点吸附到网格不能把「能压」变成「只能拒绝」。"""
    messages = [msg("user", padded(2_000, "o")), msg("assistant", padded(2_000, "r")), msg("user", "latest")]
    budget = budget_with_window(1_000)  # 上限约 349 token，两条 2,000 字符 ≈ 1,013

    plan = await compact_history("test/model", messages, budget)

    assert plan.strategy == "summarize"
    assert plan.dropped_messages == 2
    assert plan.messages == [messages[-1]]


# ---------------------------------------------------------------------
# 4. 摘要的降级路径
# ---------------------------------------------------------------------
def _long_history() -> list[ChatMessage]:
    messages = []
    for index in range(6):
        messages.append(msg("user", f"Q{index} " + padded(2_990, "q")))
        messages.append(msg("assistant", f"A{index} " + padded(2_990, "r")))
    messages.append(msg("user", "latest question"))
    return messages


def _medium_history() -> list[ChatMessage]:
    """12 条 2,000 字符（≈500 token/条）+ 本轮问题。

    量级是算好的：窗口 4,000 时切点 8 只保留 5 条（≈2,500 token）就够用，
    所以第一轮只会呼叫一次摘要；下一轮多两条时同一个切点依旧够用。
    """
    messages = []
    for index in range(6):
        messages.append(msg("user", f"Q{index} " + padded(1_990, "q")))
        messages.append(msg("assistant", f"A{index} " + padded(1_990, "r")))
    messages.append(msg("user", "latest question"))
    return messages


async def test_summarizer_exception_falls_back_to_extractive():
    calls: list[str] = []

    async def boom(transcript: str) -> str:
        calls.append(transcript)
        raise RuntimeError("上游挂了")

    plan = await compact_history("test/model", _long_history(), budget_with_window(4_000), summarizer=boom)

    assert calls  # 确实尝试过调用摘要器
    assert plan.strategy == "summarize"
    # 摘要仍然可用：退回抽取式，而不是整轮失败
    assert plan.digest is not None
    assert DIGEST_HEADER in plan.digest


async def test_empty_summary_falls_back_to_extractive():
    async def empty(transcript: str) -> str:
        return "   "

    plan = await compact_history("test/model", _long_history(), budget_with_window(4_000), summarizer=empty)

    assert plan.strategy == "summarize"
    assert plan.digest is not None
    assert DIGEST_HEADER in plan.digest


async def test_summarizer_timeout_falls_back(monkeypatch):
    """摘要发生在流式生成之前，用户在这里干等：挂住的上游不能拖住整轮。"""
    monkeypatch.setattr(context_compaction, "SUMMARY_TIMEOUT_SECONDS", 0.01)

    async def hangs(transcript: str) -> str:
        import asyncio

        await asyncio.sleep(10)
        return "- 不该到这里"

    plan = await compact_history("test/model", _long_history(), budget_with_window(4_000), summarizer=hangs)

    assert plan.strategy == "summarize"
    assert plan.digest is not None
    assert DIGEST_HEADER in plan.digest


def test_extractive_digest_is_delimited_and_bounded():
    messages = [msg("user", padded(20_000, "q")), msg("assistant", padded(20_000, "r"))]

    digest = extractive_digest(messages)

    assert digest.startswith(DIGEST_HEADER)
    assert digest.endswith(DIGEST_FOOTER)
    assert len(digest) <= MAX_DIGEST_CHARS


async def test_summary_input_keeps_both_ends_of_the_dropped_range():
    """摘要要替整段历史当记忆：会话开头的约束不能是第一个被丢掉的。

    「从最新往回填、超限即 break」时，长会话里最早那条（用户的原始约束/偏好）整条不见，
    而它恰恰是模型后面最容易违反的那一条。
    """
    seen: list[str] = []

    async def capture(transcript: str) -> str:
        seen.append(transcript)
        return "- 摘要"

    dropped = [
        msg("user", "OLDEST-CONSTRAINT " + padded(9_000, "o")),
        msg("assistant", padded(9_000, "m")),
        msg("user", padded(9_000, "m")),
        msg("assistant", padded(9_000, "m")),
        msg("user", padded(9_000, "m")),
        msg("assistant", padded(9_000, "n") + " NEWEST-DECISION"),
    ]

    await build_digest("test/model", dropped, capture)

    transcript = seen[0]
    assert "OLDEST-CONSTRAINT" in transcript
    assert "NEWEST-DECISION" in transcript
    assert len(transcript) <= MAX_SUMMARY_INPUT_CHARS


def test_extractive_digest_keeps_both_ends_when_space_is_short():
    """抽取式兜底要与模型摘要覆盖同一区间，否则两条降级路径丢的东西正好相反。"""
    messages = [msg("user", "OLDEST-CONSTRAINT " + padded(200, "o"))]
    messages.extend(msg("user", f"中间-{index} " + padded(200, "m")) for index in range(58))
    messages.append(msg("assistant", padded(200, "n") + " NEWEST-DECISION"))

    digest = extractive_digest(messages)

    assert "OLDEST-CONSTRAINT" in digest
    assert "NEWEST-DECISION" in digest
    assert len(digest) <= MAX_DIGEST_CHARS


async def test_summary_calls_share_a_single_deadline(monkeypatch):
    """候选切点最多 4 个、每个各自 20s 超时就是最坏 80s 干等，而用户这段时间什么都看不到。

    挂住的摘要器只该被调一次：deadline 用尽后，剩下的候选直接走抽取式兜底。
    """
    monkeypatch.setattr(context_compaction, "SUMMARY_TIMEOUT_SECONDS", 0.2)
    calls: list[str] = []

    async def hangs(transcript: str) -> str:
        calls.append(transcript)
        await asyncio.sleep(10)
        return "- 不该到这里"

    started = time.monotonic()
    plan = await compact_history("test/model", _long_history(), budget_with_window(4_000), summarizer=hangs)
    elapsed = time.monotonic() - started

    assert len(calls) == 1, "deadline 用尽后不该再呼叫模型"
    # 时间断言只是粗筛（不随机器抖动）：旧行为是 3 个候选各等一个超时 = 0.6s
    assert elapsed < 0.5
    assert plan.strategy == "summarize"
    assert plan.digest_kind == "extractive"


async def test_digest_kind_distinguishes_model_digest_from_fallback():
    """两条降级路径都标 `strategy="summarize"`：没有 `digest_kind` 就分不清正常与兜底。"""

    async def answering(transcript: str) -> str:
        return "- 用户在做上下文压缩"

    async def boom(transcript: str) -> str:
        raise RuntimeError("上游挂了")

    model_plan = await compact_history("test/model", _long_history(), budget_with_window(4_000), summarizer=answering)
    reset_digest_cache()
    fallback_plan = await compact_history("test/model", _long_history(), budget_with_window(4_000), summarizer=boom)

    assert model_plan.digest_kind == "model"
    assert fallback_plan.digest_kind == "extractive"
    assert model_plan.strategy == fallback_plan.strategy == "summarize"


async def test_summary_is_cached_across_calls():
    """同一段历史连续几轮都会被压缩：缓存把「每轮一次摘要调用」降到「首次一次」。"""
    calls = 0

    async def counting(transcript: str) -> str:
        nonlocal calls
        calls += 1
        return "- 用户在做上下文压缩\n- 待办：补测试"

    dropped = [msg("user", padded(3_000, "q")), msg("assistant", padded(3_000, "r"))]

    first = await build_digest("test/model", dropped, counting)
    second = await build_digest("test/model", dropped, counting)

    assert first == second
    assert calls == 1


async def test_digest_cache_is_keyed_by_the_dropped_range():
    """缓存按「折叠区间的边界 + 模型名」做键，而不是渲染后的文本。

    渲染文本每轮都在变（保留区随新消息往后移），拿它做键等于稳态每轮必 miss——
    每轮都在关键路径上多付一次完整模型调用。
    """
    calls = 0

    async def counting(transcript: str) -> str:
        nonlocal calls
        calls += 1
        return "- 早期讨论的约束"

    dropped = [msg("user", padded(3_000, "q")), msg("assistant", padded(3_000, "r"))]

    first = await build_digest("test/model", dropped, counting, dropped_range=(0, 8))
    second = await build_digest("test/model", dropped, counting, dropped_range=(0, 8))

    assert calls == 1
    assert first == second
    assert first.kind == "model"


def test_split_snaps_to_a_fixed_grid_so_consecutive_turns_reuse_the_same_range():
    """切点吸附到固定网格：只追加了两条消息的下一轮算出同一个切点。"""
    first_turn = _long_history()
    second_turn = [*first_turn, msg("assistant", "新回答"), msg("user", "再追问")]

    assert context_compaction._split_keep_recent(first_turn, 4) == context_compaction._split_keep_recent(second_turn, 4)


async def test_the_next_turn_reuses_the_previous_digest_instead_of_paying_twice():
    """稳态（每轮都跨窗口）里，第二轮只该很便宜——同一个切点应直接命中上一轮的摘要。

    `_medium_history` 的量级是算好的：窗口 4,000 时切点 8 保留的 7 条（≈2,000 token）
    加上摘要仍在 3,049 的可用上限内，所以第一轮只会呼叫一次。第二轮多两条消息后，
    切点不吸附时会滑到 10（新区间、必 miss、再付一次完整模型调用），吸附到网格则仍是 8。
    """
    calls = 0

    async def counting(transcript: str) -> str:
        nonlocal calls
        calls += 1
        return f"- 第 {calls} 次摘要"

    first_turn = _medium_history()
    await compact_history("test/model", first_turn, budget_with_window(4_000), summarizer=counting)
    assert calls == 1

    second_turn = [*first_turn, msg("assistant", "新回答"), msg("user", "再追问")]
    plan = await compact_history("test/model", second_turn, budget_with_window(4_000), summarizer=counting)

    assert calls == 1, "同一区间必须命中上一轮的摘要，而不是每轮重打一次"
    assert plan.strategy == "summarize"
    assert plan.digest_kind == "model"


async def test_digest_is_not_shared_across_models():
    """不同模型的摘要风格不同：缓存键混用会让「换了模型之后摘要跳变」无法解释。"""
    calls = 0

    async def counting(transcript: str) -> str:
        nonlocal calls
        calls += 1
        return f"- 第 {calls} 次"

    dropped = [msg("user", padded(3_000, "q"))]

    await build_digest("model/a", dropped, counting)
    await build_digest("model/b", dropped, counting)

    assert calls == 2


async def test_no_summarizer_uses_extractive_digest():
    """摘要器缺席（模型不可用/装配失败）是受支持的降级路径，不是错误。"""
    plan = await compact_history("test/model", _long_history(), budget_with_window(4_000), summarizer=None)

    assert plan.strategy == "summarize"
    assert plan.digest is not None
    assert DIGEST_HEADER in plan.digest


async def test_summary_over_budget_is_capped():
    """摘要是压缩产物，它自己不能又变成一段超长文本。"""

    async def verbose(transcript: str) -> str:
        return "- " + padded(20_000, "x")

    plan = await compact_history("test/model", _long_history(), budget_with_window(4_000), summarizer=verbose)

    assert plan.digest is not None
    assert len(plan.digest) <= MAX_DIGEST_CHARS


# ---------------------------------------------------------------------
# 5. 压缩后的判定与文案
# ---------------------------------------------------------------------
async def test_plan_reports_over_limit_when_compaction_is_not_enough():
    """压不动时必须如实报告，让调用方走拒绝分支——而不是把超限历史发上去。"""
    # 一条巨长的最后消息：既不能截断（是本轮问题），也没有别的可折叠
    messages = [msg("user", padded(31_000, "q"))]
    budget = budget_with_window(1_000)

    plan = await compact_history("test/model", messages, budget)

    assert plan.over_limit is True


async def test_plan_not_over_limit_when_truncation_suffices():
    messages = [msg("user", padded(20_000, "o")), msg("assistant", padded(20_000, "r")), msg("user", "q")]
    budget = budget_with_window(4_000)

    plan = await compact_history("test/model", messages, budget)

    assert plan.over_limit is False


# ---------------------------------------------------------------------
# 与预算口径的接线
# ---------------------------------------------------------------------
async def test_compaction_rejudges_with_estimate_basis():
    """压缩后一律按估算口径重判：锚点已失效，锚点口径的上限也偏宽。

    这里用同一份历史造一个 `anchor` 口径的预算：它的 `limit_tokens` 不扣固定开销，
    比 `estimate` 口径宽。若压缩沿用那份上限，就会高估可用空间。
    """
    messages = [msg("user", padded(20_000, "o")), msg("assistant", padded(20_000, "r")), msg("user", "q")]
    used = estimate_messages_tokens(messages)
    anchor_budget = build_budget(
        model="test/model",
        context_window=200_000,
        max_output_tokens=1024,
        used_tokens=used,
        overhead_tokens=TEST_OVERHEAD,
        basis="anchor",
        used_tokens_include_overhead=True,
    )

    plan = await compact_history("test/model", messages, anchor_budget)

    # 上限换成了 estimate 口径：扣固定开销后一定不大于锚点口径的上限
    assert plan.limit_tokens <= anchor_budget.limit_tokens


def test_module_exports_are_stable():
    """压缩入口只有这几个：调用方不该去碰内部的分级实现。"""
    assert "compact_history" in context_compaction.__all__
    assert "truncate_middle" in context_compaction.__all__
    assert "reset_digest_cache" in context_compaction.__all__
