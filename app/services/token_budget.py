"""上下文窗口预算：粗估 token 用量，并给出本轮历史可用的上限。

为什么需要这个模块：历史是客户端**全量回放**上来的（`chat_service.convert_chat_history`），
而此前唯一的保护是字符上限（`app/schemas/chat.py:MAX_HISTORY_CHARS = 400_000`）。
字符与 token 不是一个量纲——40 万中文字符在常见分词器下已经是 30 万 token 以上，
对 32k / 128k 窗口的模型早就溢出了。溢出的请求照样发得出去，由上游以 400 拒绝，
`upstream_errors` 把它映射成「模型不可用，请更换模型」：归因是错的，用户拿到的
自救动作也是错的（400 既不会自动重试，也不在备用模型的白名单里）。

估算刻意不引入分词器依赖（tiktoken 之类）：

1. 准确度对这里是过剩的——要判断的是「两万还是三十万」这个量级，不是逐 token 账目；
2. 真正的分词器要绑定具体模型的词表，而网关暴露的模型 id 与上游词表并不同名，
   拿错词表反而给出一个看着很精确的错数；
3. 估算偏高是安全方向（早一点提示），偏低才会漏掉溢出，所以每个常数都往大取。

上游没报 `context_window` 时**不做判定**（返回 `None`，放行）：与
`model_service.get_known_model_ids` 的取舍一致——拿不到数据时宁可放行，
也不要拿一个编出来的窗口去拦用户的请求。
"""

import logging
from dataclasses import dataclass
from typing import Literal

from app.config import settings
from app.schemas.chat import ChatMessage, ContextAnchor
from app.services.agent_factory import render_system_prompt
from app.services.model_service import get_model_limits

logger = logging.getLogger(__name__)

#: 单条消息的结构性开销（role 标记、消息分隔符）。多数 chat 模板每条 3~5 token。
MESSAGE_OVERHEAD_TOKENS = 4

#: 工具 JSON schema 的固定开销。工具集是运行时装配的（tavily 没配 key 就不挂），
#: 精确估算需要复刻 pydantic-ai 的 schema 生成，收益不抵复杂度；这里按当前工具集的
#: 量级取一个常量，方向同样是「宁可多留」。
TOOL_SCHEMA_OVERHEAD_TOKENS = 512

#: 非 ASCII 文本每 token 覆盖的字符数。常见分词器对英文约 4 字符/token，
#: 对中日韩文字接近 1 字符/token —— 两者分开算，而不是整段一律按 4 字符折算
#: （那会把中文用量低估到四分之一，正是需要拦住的场景）。
ASCII_CHARS_PER_TOKEN = 4


def _is_wide_char(char: str) -> bool:
    """是否属于「一个字符接近一个 token」的文字（中日韩 + 全角标点）。

    用区间判断而不是 `unicodedata.east_asian_width`：后者包含 Ambiguous 类别
    （Greek、Cyrillic 等），按宽字符算会把一大片西文用量翻四倍。
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

    宽字符按 1 token/字计，其余按 4 字符/token 向上取整。这个口径对中文略偏高
    （多数分词器常见汉字约为 0.6~1 token），偏高是刻意的安全方向。
    """
    if not text:
        return 0

    wide = 0
    narrow = 0
    for char in text:
        if _is_wide_char(char):
            wide += 1
        else:
            narrow += 1

    return wide + -(-narrow // ASCII_CHARS_PER_TOKEN)


def estimate_messages_tokens(messages: list[ChatMessage]) -> int:
    """估算整份历史的 token 数（含逐条结构性开销）。

    `thinking` 也计入：`convert_chat_history` 会把它重建成 `ThinkingPart` 一并发给
    上游，因此它同样占窗口。前端当前不回放思考链，但后端不能依赖这一点。
    """
    total = 0
    for message in messages:
        total += MESSAGE_OVERHEAD_TOKENS
        total += estimate_tokens(message.content)
        total += estimate_tokens(message.thinking or "")
    return total


def resolve_reserve_tokens(context_window: int, max_output_tokens: int | None) -> int:
    """本轮的预留 token：留给模型写回复的空间。

    优先用上游声明的 `max_output_tokens`，没有则用配置里的默认值。无论取哪个都
    再压一道 `context_window // 2` 的上限——部分网关把 `max_output_tokens` 报成与
    窗口同量级（例如窗口 32k、最大输出也报 32k），照扣会把可用历史压成 0，
    等于把所有请求都判成超限。
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
    """一次请求的上下文预算快照。

    纯数据（不在属性里读 settings）：它的三个消费方——拒绝判定、裁剪规划、测试——
    需要的是同一份数字，把计算塞进属性会让「同一个对象读两次得到两个值」。
    `limit_tokens` 已经含了安全系数，因此直接与 `used_tokens` 比较即可；它按
    `used_tokens` 的口径算出：`used_tokens` 不含固定开销时，这部分已从可用空间里扣掉
    （见 `build_budget`），否则两边会各算一次。

    `basis` 说明 `used_tokens` 是怎么来的：`estimate`（从零估算整份历史）还是
    `anchor`（沿用上一轮的真实用量 + 只估算新增消息）。两者相差可能很大，所以它
    必须跟着日志和文案走，否则现场无法解释「为什么这次判得比上次宽」。
    """

    model: str
    context_window: int
    max_output_tokens: int | None
    reserve_tokens: int
    overhead_tokens: int
    limit_tokens: int
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
    """把各项凑成一个预算快照。单列成函数是为了让「上限怎么算的」只有一处。

    `used_tokens_include_overhead` 说明 `used_tokens` 里有没有系统提示词与工具 schema
    这部分固定开销，并据此决定它还要不要再从可用空间里扣一次：

    - `False`：`used_tokens` 只是 messages 的估算，固定开销还没算进去，从可用空间扣；
    - `True`：`used_tokens` 已经是整份真实 prompt 的规模（锚点值），再扣就是扣两次。

    两条路径的 `used_tokens` 因此都对得上「下一次真实 prompt 有多大」，与同一个窗口
    比较的才是同一件事。
    """
    reserve_tokens = resolve_reserve_tokens(context_window, max_output_tokens)
    overhead_outside_used = 0 if used_tokens_include_overhead else overhead_tokens
    available = context_window - reserve_tokens - overhead_outside_used
    limit_tokens = max(int(available * settings.context_safety_ratio), 0)
    return ContextBudget(
        model=model,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        reserve_tokens=reserve_tokens,
        overhead_tokens=overhead_tokens,
        limit_tokens=limit_tokens,
        used_tokens=used_tokens,
        basis=basis,
    )


def usable_anchor(messages: list[ChatMessage], anchor: ContextAnchor | None) -> ContextAnchor | None:
    """判断这个锚点能不能用；不能用返回 None（退回到从零估算）。

    两个条件都必需：

    1. 下标在范围内，且指向一条 **assistant** 消息——用量是那一轮回答产生的，指向
       user 消息说明客户端算错了位置，此时「锚点之后的增量」根本不对应任何真实区间；
    2. `context_tokens` 为正（schema 已经拦了，但本函数是公开的，这里自己再保一道）。

    不能用时**不报错**：锚点只是精度提升，客户端版本错位/历史被裁剪都不该让对话
    失败——退回估算即可。但要留一条日志，否则「锚点没生效」会静默变成「预算怎么
    忽然变宽了」。
    """
    if anchor is None:
        return None
    if not 0 <= anchor.message_index < len(messages):
        logger.warning("上下文锚点下标越界，退回估算 (index=%d, messages=%d)", anchor.message_index, len(messages))
        return None
    if messages[anchor.message_index].role != "assistant":
        logger.warning("上下文锚点指向的不是 assistant 消息，退回估算 (index=%d)", anchor.message_index)
        return None
    return anchor


async def resolve_context_budget(
    model_name: str,
    messages: list[ChatMessage],
    anchor: ContextAnchor | None = None,
) -> ContextBudget | None:
    """解析本轮的上下文预算；无法判定时返回 `None`。

    返回 `None` 的三种情形都意味着**放行**：

    1. 守卫被配置关掉（`context_guard_mode = "off"`）；
    2. 网关没报这个模型的 `context_window`（没数据就不猜）；
    3. 模型不在网关列表里（白名单已放行过未知 id，这里不应反悔）。

    给了可用的锚点就不再从零估算历史，但两条 basis 的 `used_tokens` 口径不同，
    `build_budget` 会据此对齐上限：

    - `anchor`：`context_tokens` 是**产生锚点那条助手消息的那次请求**的 prompt，已含
      系统提示词与工具 schema，但不含那条助手消息本身（它是那次请求的输出）。因此估算
      从 `message_index` 起，把锚点那条回答也补进来——否则一整轮输出既不在真实值里、
      也没被估算。这个 used 就是真实 prompt 的规模，**不再**扣固定开销。
    - `estimate`：从零估算的 messages 用量，**不含**固定开销，由 `build_budget` 从
      可用空间里扣掉。
    """
    if settings.context_guard_mode == "off":
        return None

    context_window, max_output_tokens = await get_model_limits(model_name)
    if context_window is None or context_window <= 0:
        _warn_unknown_window_once(model_name)
        return None

    # 系统提示词 + 工具 schema 也要占窗口，且它们是每轮都发的固定开销。它算不算在
    # `used_tokens` 里由下面两条路径决定，不在这里一刀切地扣掉（见 build_budget）。
    overhead_tokens = estimate_tokens(render_system_prompt()) + TOOL_SCHEMA_OVERHEAD_TOKENS

    anchor = usable_anchor(messages, anchor)
    if anchor is not None:
        # 从锚点那条（assistant）起算：锚点值是产生它的那次请求的 prompt，不含它自己。
        used_tokens = anchor.context_tokens + estimate_messages_tokens(messages[anchor.message_index :])
        used_tokens_include_overhead = True
        basis: Literal["estimate", "anchor"] = "anchor"
    else:
        used_tokens = estimate_messages_tokens(messages)
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


#: 已告警过的「网关没报窗口」的模型 id。按 id 去重：这类告警每个请求都会触发，
#: 不去重会把日志刷满，反而盖住真正的问题（与 `chat_service._warn_unknown_once` 同理）。
_WARNED_UNKNOWN_WINDOWS: set[str] = set()


def _warn_unknown_window_once(model_name: str) -> None:
    if model_name in _WARNED_UNKNOWN_WINDOWS:
        return
    _WARNED_UNKNOWN_WINDOWS.add(model_name)
    logger.warning(
        "网关未提供模型 %s 的 context_window，跳过上下文预算判定（本轮及后续都不拦）",
        model_name,
    )


def overflow_message(budget: ContextBudget) -> str:
    """整份历史超限时给用户的文案。

    必须带三个数字（用量、上限、模型窗口）与两个动作：没有数字，用户不知道该删多少；
    没有动作，这轮对话就是死路——他的消息已经落库，下一轮只会更超。
    """
    return (
        f"本会话上下文约 {budget.used_tokens:,} tokens，超出模型 {budget.model} 的可用上限 "
        f"{budget.limit_tokens:,}（该模型窗口 {budget.context_window:,}）。"
        "请新建会话后继续提问，或改用上下文窗口更大的模型。"
    )


__all__ = [
    "ContextBudget",
    "MESSAGE_OVERHEAD_TOKENS",
    "TOOL_SCHEMA_OVERHEAD_TOKENS",
    "build_budget",
    "estimate_messages_tokens",
    "estimate_tokens",
    "overflow_message",
    "resolve_context_budget",
    "resolve_reserve_tokens",
    "usable_anchor",
]
