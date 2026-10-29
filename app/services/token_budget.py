"""上下文窗口预算：粗估 token 用量，并给出本轮历史可用的上限。

历史由客户端全量回放，只靠 40 万字符上限兜底，而中文早已溢出 32k/128k 窗口，溢出会被
上游 400 拒绝并映射成错误的「模型不可用」。估算刻意不接分词器：要的是量级，且偏高是安全方向。
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from app.config import settings
from app.schemas.chat import ChatMessage, ContextAnchor
from app.services.agent_factory import render_system_prompt
from app.services.model_service import get_model_limits

logger = logging.getLogger(__name__)

#: 单条消息的结构性开销（role 标记、消息分隔符）。多数 chat 模板每条 3~5 token。
MESSAGE_OVERHEAD_TOKENS = 4

#: 工具 JSON schema 的固定开销：工具集运行时装配，精确估算要复刻 pydantic-ai 的生成
#: 逻辑，不值；按当前工具集量级取常量，方向同样是「宁可多留」。
TOOL_SCHEMA_OVERHEAD_TOKENS = 512

#: 非 ASCII 文本每 token 覆盖的字符数（英文约 4，中日韩接近 1）。两者分开算，而不是
#: 整段一律按 4 折算——那会把中文用量低估到四分之一，正是要拦的场景。
ASCII_CHARS_PER_TOKEN = 4

#: 单次估算的字符数阈值：超过就把估算挪进线程。40 万字符的逐字符循环要 300~500ms，足够让
#: 同进程其它对话明显卡顿；阈值也不能太小，`asyncio.to_thread` 自身要付线程池调度开销。
THREAD_ESTIMATE_CHARS_THRESHOLD = 50_000


def _is_wide_char(char: str) -> bool:
    """是否属于「一个字符接近一个 token」的文字（中日韩 + 全角标点）。

    用区间判断而非 `unicodedata.east_asian_width`：后者含 Ambiguous 类别（Greek、
    Cyrillic 等），按宽字符算会把一大片西文用量翻四倍。
    """
    code = ord(char)
    return (
        0x1100 <= code <= 0x11FF  # 谚文字母
        or 0x2E80 <= code <= 0x303F  # 中日韩部首、标点
        or 0x3040 <= code <= 0x30FF  # 假名
        or 0x3400 <= code <= 0x4DBF  # 汉字扩展 A
        or 0x4E00 <= code <= 0x9FFF  # 汉字
        or 0xAC00 <= code <= 0xD7AF  # 谚文音节
        or 0xF900 <= code <= 0xFAFF  # 汉字兼容
        or 0xFE30 <= code <= 0xFE4F  # 兼容形式的标点
        or 0xFF00 <= code <= 0xFF60  # 全角 ASCII
        or 0xFFE0 <= code <= 0xFFE6  # 全角符号
        or 0x20000 <= code <= 0x3FFFF  # 汉字扩展 B 及以上
    )


def estimate_tokens(text: str) -> int:
    """粗估一段文本的 token 数；空串为 0。

    宽字符 1 token/字，其余 4 字符/token 向上取整，略偏高（安全方向）。纯 ASCII 走 `isascii()`
    快路径（微秒级），含任一非 ASCII 字符即退到 O(n) 逐字符循环，所以别卡事件循环的责任在调用方。
    """
    if not text:
        return 0

    # 纯 ASCII 全是窄字符，交给 C 层的 isascii() 一次判定，不必逐字符走 Python 循环。
    # 无锚点时每轮都要对整份历史跑一遍，正是这里把大输入挪进线程的原因。
    if text.isascii():
        return -(-len(text) // ASCII_CHARS_PER_TOKEN)

    wide = 0
    narrow = 0
    for char in text:
        if _is_wide_char(char):
            wide += 1
        else:
            narrow += 1

    return wide + -(-narrow // ASCII_CHARS_PER_TOKEN)


def estimate_messages_tokens(messages: list[ChatMessage]) -> int:
    """估算整份历史的 token 数（含逐条结构性开销）；`thinking` 也占窗口，必须计入。

    同步函数：异步调用方走 `_estimate_messages_tokens_off_loop`，那里才按量决定要不要让出
    事件循环。
    """
    total = 0
    for message in messages:
        total += MESSAGE_OVERHEAD_TOKENS
        total += estimate_tokens(message.content)
        total += estimate_tokens(message.thinking or "")
    return total


def resolve_reserve_tokens(context_window: int, max_output_tokens: int | None) -> int:
    """本轮的预留 token：优先用上游声明的 `max_output_tokens`，否则用配置默认值。

    无论取哪个都再压一道 `context_window // 2`：部分网关把最大输出报成与窗口同量级（32k 窗口
    报 32k 输出），照扣会把可用历史压成 0，所有请求都判超限。
    """
    declared = (
        max_output_tokens
        if isinstance(max_output_tokens, int) and not isinstance(max_output_tokens, bool) and max_output_tokens > 0
        else None
    )
    reserve = declared if declared is not None else settings.context_reserve_tokens
    return max(min(reserve, context_window // 2), 0)


@dataclass(frozen=True)
class ContextBudget:
    """一次请求的预算快照（纯数据，不在属性里读 settings，否则同一对象两次读得到两个值）。

    `limit_tokens` 已含安全系数，直接与 `used_tokens` 比；`prompt_limit_tokens` 是真实 prompt
    口径（含系统提示词与工具 schema），前端画进度条只能拿它当分母。`basis` 说明 used 的来源。
    """

    model: str
    context_window: int
    max_output_tokens: int | None
    reserve_tokens: int
    overhead_tokens: int
    limit_tokens: int
    prompt_limit_tokens: int
    used_tokens: int
    basis: Literal["estimate", "anchor"] = "estimate"

    @property
    def over_limit(self) -> bool:
        return self.used_tokens > self.limit_tokens

    @property
    def remaining_tokens(self) -> int:
        """还能再装多少 token；已经超限时为负数。"""
        return self.limit_tokens - self.used_tokens


def build_budget(
    model: str,
    context_window: int,
    max_output_tokens: int | None,
    used_tokens: int,
    overhead_tokens: int,
    basis: Literal["estimate", "anchor"] = "estimate",
    *,
    used_tokens_include_overhead: bool,
) -> ContextBudget:
    """把各项凑成预算快照；「上限怎么算的」只有这一处。

    `used_tokens_include_overhead` 决定固定开销还要不要再扣一次：`True` 表示 used 已是整份
    真实 prompt（锚点值），再扣就是扣两次；两条路径的 used 都要对得上「下次真实 prompt 有多大」。
    """
    reserve_tokens = resolve_reserve_tokens(context_window, max_output_tokens)
    overhead_outside_used = 0 if used_tokens_include_overhead else overhead_tokens
    # 真实 prompt 的口径：固定开销已经在 used_tokens/上限里，只扣回答预留
    prompt_limit_tokens = max(int((context_window - reserve_tokens) * settings.context_safety_ratio), 0)
    available = context_window - reserve_tokens - overhead_outside_used
    limit_tokens = max(int(available * settings.context_safety_ratio), 0)
    return ContextBudget(
        model=model,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        reserve_tokens=reserve_tokens,
        overhead_tokens=overhead_tokens,
        limit_tokens=limit_tokens,
        prompt_limit_tokens=prompt_limit_tokens,
        used_tokens=used_tokens,
        basis=basis,
    )


def rebuild_budget_as_estimate(budget: ContextBudget, used_tokens: int) -> ContextBudget:
    """把一份预算换成「从零估算」口径，上限跟着换。

    压缩会改变消息下标，客户端的锚点随即失效，规模只能从零估算；而 anchor 口径的 used 已含
    固定开销、limit 没再扣一次，直接沿用会得到偏宽的上限，压缩可能「判成够了却还是超」。
    """
    return build_budget(
        model=budget.model,
        context_window=budget.context_window,
        max_output_tokens=budget.max_output_tokens,
        used_tokens=used_tokens,
        overhead_tokens=budget.overhead_tokens,
        basis="estimate",
        used_tokens_include_overhead=False,
    )


#: 锚点被拒的原因 → 已告警。客户端版本错位时每轮都带同一个坏锚点，逐轮告警会刷满日志
#: （与 `_WARNED_UNKNOWN_WINDOWS` 同一套路）；原因只有三种，容量固定。
_WARNED_ANCHOR_REASONS: dict[str, None] = {}


def reset_token_budget_warnings() -> None:
    """清空两处「只告警一次」的去重集合（测试用；也可用于「改了配置想再看一次告警」）。"""
    _WARNED_ANCHOR_REASONS.clear()
    _WARNED_UNKNOWN_WINDOWS.clear()


def _warn_anchor_dropped_once(reason: str, template: str, *args: object) -> None:
    if reason in _WARNED_ANCHOR_REASONS:
        return
    _WARNED_ANCHOR_REASONS[reason] = None
    logger.warning(template, *args)


def usable_anchor(messages: list[ChatMessage], anchor: ContextAnchor | None) -> ContextAnchor | None:
    """判断这个锚点能不能用；不能用返回 None，退回从零估算。

    三个条件缺一不可：下标指向范围内的 assistant 消息、`context_tokens` 为正、且大过它声称覆盖
    的消息结构性开销——否则填个小值就能跳过判定。只按条数设下限，更精细的偏小值由调用方核对。
    """
    if anchor is None:
        return None
    if not 0 <= anchor.message_index < len(messages):
        # 同一原因只记一次：坏锚点通常来自客户端版本错位，会随每一条消息重复出现
        _warn_anchor_dropped_once(
            "index",
            "上下文锚点下标越界，退回估算 (index=%d, messages=%d)",
            anchor.message_index,
            len(messages),
        )
        return None
    if messages[anchor.message_index].role != "assistant":
        _warn_anchor_dropped_once(
            "role",
            "上下文锚点指向的不是 assistant 消息，退回估算 (index=%d)",
            anchor.message_index,
        )
        return None
    floor_tokens = MESSAGE_OVERHEAD_TOKENS * (anchor.message_index + 1)
    if anchor.context_tokens <= floor_tokens:
        _warn_anchor_dropped_once(
            "floor",
            "上下文锚点低于其覆盖消息的结构性开销，退回估算 (index=%d, context_tokens=%d, 下限=%d)",
            anchor.message_index,
            anchor.context_tokens,
            floor_tokens,
        )
        return None
    return anchor


def _history_chars(messages: Sequence[ChatMessage]) -> int:
    """待估算历史里 `content + thinking` 的字符总数。

    只求 `len`、不做逐字符判定，所以它自己在多大输入下都是微秒级，可以拿它决定要不要挪线程。
    """
    return sum(len(message.content) + len(message.thinking or "") for message in messages)


async def _estimate_messages_tokens_off_loop(messages: list[ChatMessage]) -> int:
    """按字符量决定同步估算还是放进线程；两条路径的返回值必须逐位相同。

    阈值按「这次真正要估算的那段」算：锚点路径传入的是 `messages[index:]` 切片，不是整份历史。
    """
    if _history_chars(messages) > THREAD_ESTIMATE_CHARS_THRESHOLD:
        return await asyncio.to_thread(estimate_messages_tokens, messages)
    return estimate_messages_tokens(messages)


async def resolve_context_budget(
    model_name: str,
    messages: list[ChatMessage],
    anchor: ContextAnchor | None = None,
    *,
    model_limits: tuple[int | None, int | None] | None = None,
) -> ContextBudget | None:
    """解析本轮的上下文预算；返回 `None` 表示无法判定，一律放行（守卫关闭/窗口未知/未收录）。

    `model_limits` 传调用方已解析的值，避免同一请求扫两遍库、且两次读之间改配置会让预算与实际
    模型对不上（`None` 表示自己查）。锚点路径从 `message_index` 起算，口径由 `build_budget` 对齐。
    """
    if settings.context_guard_mode == "off":
        return None

    if model_limits is None:
        context_window, max_output_tokens = await get_model_limits(model_name)
    else:
        context_window, max_output_tokens = model_limits
    if context_window is None or context_window <= 0:
        _warn_unknown_window_once(model_name)
        return None

    # 系统提示词 + 工具 schema 每轮都要发，也要占窗口；它算不算在 `used_tokens` 里由
    # 下面两条路径决定，不在这里一刀切地扣（见 build_budget）。
    overhead_tokens = estimate_tokens(render_system_prompt()) + TOOL_SCHEMA_OVERHEAD_TOKENS

    anchor = usable_anchor(messages, anchor)
    if anchor is not None:
        # 锚点来自客户端，不能无条件相信：声称覆盖 N 条消息却报一个远小于前缀真实规模的
        # token 数，就是拿它绕过上下文预检（I-08）。用前缀估算核对，明显偏低就退回全量估算。
        prefix_estimate = await _estimate_messages_tokens_off_loop(messages[: anchor.message_index + 1])
        if anchor.context_tokens < int(prefix_estimate * 0.9):
            _warn_anchor_dropped_once(
                "understated",
                "上下文锚点明显低于前缀估算，退回估算 (index=%d, context_tokens=%d, 前缀估算=%d)",
                anchor.message_index,
                anchor.context_tokens,
                prefix_estimate,
            )
            anchor = None

    if anchor is not None:
        # 从锚点那条（assistant）起算：锚点值是产生它的那次请求的 prompt，不含它自己。
        used_tokens = anchor.context_tokens + await _estimate_messages_tokens_off_loop(messages[anchor.message_index :])
        used_tokens_include_overhead = True
        basis: Literal["estimate", "anchor"] = "anchor"
    else:
        used_tokens = await _estimate_messages_tokens_off_loop(messages)
        used_tokens_include_overhead = False
        basis = "estimate"

    return build_budget(
        model=model_name,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        used_tokens=used_tokens,
        overhead_tokens=overhead_tokens,
        basis=basis,
        used_tokens_include_overhead=used_tokens_include_overhead,
    )


#: 已告警过的「网关没报窗口」的模型 id，按 id 去重（每个请求都触发，不去重会刷满日志）。
#: 必须有界：网关列表不可用时任意 `model_name` 都会被放行，故用固定容量 dict 当 LRU。
_WARNED_UNKNOWN_WINDOWS_MAX = 256
_WARNED_UNKNOWN_WINDOWS: dict[str, None] = {}


def _warn_unknown_window_once(model_name: str) -> None:
    if model_name in _WARNED_UNKNOWN_WINDOWS:
        return
    if len(_WARNED_UNKNOWN_WINDOWS) >= _WARNED_UNKNOWN_WINDOWS_MAX:
        # dict 保插入顺序：弹出的就是最早那条
        _WARNED_UNKNOWN_WINDOWS.pop(next(iter(_WARNED_UNKNOWN_WINDOWS)))
    _WARNED_UNKNOWN_WINDOWS[model_name] = None
    logger.warning(
        "网关未提供模型 %s 的 context_window，跳过上下文预算判定（本轮及后续都不拦）",
        model_name,
    )


def overflow_message(budget: ContextBudget) -> str:
    """整份历史超限时给用户的文案。

    必须带用量/上限/模型窗口三个数字与两个动作：没有数字用户不知道删多少，没有动作这轮就是
    死路——消息已落库，下一轮只会更超。
    """
    return (
        f"本会话上下文约 {budget.used_tokens:,} tokens，超出模型 {budget.model} 的可用上限 "
        f"{budget.limit_tokens:,}（该模型窗口 {budget.context_window:,}）。"
        "请新建会话后继续提问，或改用上下文窗口更大的模型。"
    )


__all__ = [
    "ContextBudget",
    "MESSAGE_OVERHEAD_TOKENS",
    "rebuild_budget_as_estimate",
    "THREAD_ESTIMATE_CHARS_THRESHOLD",
    "TOOL_SCHEMA_OVERHEAD_TOKENS",
    "build_budget",
    "estimate_messages_tokens",
    "estimate_tokens",
    "overflow_message",
    "resolve_context_budget",
    "resolve_reserve_tokens",
    "usable_anchor",
]
