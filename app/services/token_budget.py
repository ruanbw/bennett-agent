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

## 长文本不阻塞事件循环

`resolve_context_budget` 是每个 `/api/chat` 请求的必经路径，而单进程服务里一次
几百毫秒的同步计算会挡住所有在飞的 SSE（估算的代价随文本长度线性增长，见
`estimate_tokens`）。所以按量分流：待估算的字符总数超过
`THREAD_ESTIMATE_CHARS_THRESHOLD` 时把估算放进 `asyncio.to_thread`，低于阈值走
同步快路径——小请求不值得付线程调度的钱。
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

#: 工具 JSON schema 的固定开销。工具集是运行时装配的（tavily 没配 key 就不挂），
#: 精确估算需要复刻 pydantic-ai 的 schema 生成，收益不抵复杂度；这里按当前工具集的
#: 量级取一个常量，方向同样是「宁可多留」。
TOOL_SCHEMA_OVERHEAD_TOKENS = 512

#: 非 ASCII 文本每 token 覆盖的字符数。常见分词器对英文约 4 字符/token，
#: 对中日韩文字接近 1 字符/token —— 两者分开算，而不是整段一律按 4 字符折算
#: （那会把中文用量低估到四分之一，正是需要拦住的场景）。
ASCII_CHARS_PER_TOKEN = 4

#: 单次估算的字符数阈值：超过它就把估算挪进线程。
#:
#: 50_000 字符在中英混排口径下约 50~65ms 的逐字符循环（本机实测），已经超过一个
#: SSE 事件间隔的量级；40 万字符则是 300~500ms，足够让同进程内其它对话明显卡顿。
#: 阈值不能取太小：`asyncio.to_thread` 每次都要走一次线程池调度（任务提交 + 唤醒），
#: 对几千字符的常规请求反而是净损失，而这类请求在 C 层快路径上只需微秒。
#: 纯 ASCII 文本即使超过阈值也不会变慢（`estimate_tokens` 的 `isascii()` 快路径），
#: 多付的只是一次线程往返——比误让百毫秒级循环占住循环更划算。
THREAD_ESTIMATE_CHARS_THRESHOLD = 50_000


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

    性能边界要写清楚，因为它的调用方是全量历史：

    - 纯 ASCII（英文、代码、JSON）走 `str.isascii()` 快路径，整个判定在 C 层一次
      完成，40 万字符也是微秒级。
    - 只要含一个非 ASCII 字符就退到逐字符循环（O(n) 次 Python 调用），40 万中文
      在本机约 330ms、中英混排约 520ms。

    没有为后者引入更快的手段：`str.translate` 预建表要一次性构造十七万条目的
    dict（约十几 MB 常驻），预编译正则也仍在百毫秒级，两者都不改变「非纯 ASCII 文本
    代价随长度线性增长」这件事；numpy 之类的新依赖对这里更是过剩。

    既然单次循环无法再压，就把「别卡住事件循环」的责任放在调用方：
    `resolve_context_budget` 在字符总量超过 `THREAD_ESTIMATE_CHARS_THRESHOLD` 时
    用 `asyncio.to_thread` 调用估算，本函数本身仍是同步的纯计算。
    """
    if not text:
        return 0

    # 纯 ASCII 时全是窄字符，口径就是 4 字符/token：交给 C 层的 isascii() 判一次，
    # 不必逐字符走 Python 循环。无锚点时每轮都要对整份历史跑一遍（网关流式不回
    # usage 就没有锚点），40 万字符的逐字符循环是百毫秒级，会挡住同一进程内其它
    # SSE——这正是 `resolve_context_budget` 把大输入挪进线程的原因。
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
    """估算整份历史的 token 数（含逐条结构性开销）。

    `thinking` 也计入：`convert_chat_history` 会把它重建成 `ThinkingPart` 一并发给
    上游，因此它同样占窗口。前端当前不回放思考链，但后端不能依赖这一点。

    同步函数：异步调用方不要直接调它，走 `_estimate_messages_tokens_off_loop`，
    那才是按量决定要不要让出事件循环的入口。
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

    `prompt_limit_tokens` 是**真实 prompt** 的口径：窗口扣掉回答预留后乘安全系数，
    不再扣固定开销（真实 prompt 里本来就已经含了它）。前端要把上游回报的
    `context_tokens`（真实 prompt，含系统提示词与工具 schema）画成进度条时只能用这个
    分母——拿原始窗口当分母会把「已经用掉 90% 预算」显示成「才用了 56%」。

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

    压缩路径必须用这个口径：压缩会改变消息的下标与条数，客户端的锚点（「截止到第
    N 条的真实用量」）随即失效，压缩后的规模只能从零估算。而两种口径的可用上限
    并不相同——`anchor` 口径的 `used_tokens` 已含固定开销，`limit_tokens` 因此没有
    再扣一次；`estimate` 口径要扣。直接拿锚点口径的上限去比估算值，会得到一个
    偏宽的上限，压缩可能「判成够了却还是超」。

    这里重算而不是简单调 `build_budget`：窗口、预留、固定开销都取自已解析的快照，
    与「本轮判定用的配置」保持同源（中途管理员改配置不会让两次计算分叉）。
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


#: 锚点被拒的原因 → 是否已告警。客户端版本错位时**每轮**都会带着同一个坏锚点，
#: 逐轮告警会把日志刷满（与 `_WARNED_UNKNOWN_WINDOWS` 同一个套路）。
#: 只有三种原因，容量固定，不存在被输入灌大的风险。
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
    """判断这个锚点能不能用；不能用返回 None（退回到从零估算）。

    三个条件都必需：

    1. 下标在范围内，且指向一条 **assistant** 消息——用量是那一轮回答产生的，指向
       user 消息说明客户端算错了位置，此时「锚点之后的增量」根本不对应任何真实区间；
    2. `context_tokens` 为正（schema 已经拦了，但本函数是公开的，这里自己再保一道）；
    3. `context_tokens` 大过它声称覆盖的 `message_index + 1` 条消息的结构性开销
       （`MESSAGE_OVERHEAD_TOKENS * (message_index + 1)`）——锚点会顶替整份历史的
       估算，所以它至少得像一份真实 prompt；否则填一个小值就能让任意长历史跳过判定。

    第 3 条是 O(1) 的，能力边界必须说清：它只拿消息条数当参照，因此**只能拦住荒谬值**
    （例如 `{message_index: 5, context_tokens: 1}`）。客户端精心构造一个「大过下限、
    却远小于真实前缀」的偏小值仍然拦不住——要拦住它就得重算前缀估算，那会把锚点
    省下的开销全部赔回去。这类输入是自伤：它只会让用户自己那一轮的请求带着超窗的
    prompt 发上去，被上游以 400 顶回来。

    不能用时**不报错**：锚点只是精度提升，客户端版本错位/历史被裁剪都不该让对话
    失败——退回估算即可。但要留一条日志，否则「锚点没生效」会静默变成「预算怎么
    忽然变宽了」。
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

    只求 `len` 而不做任何逐字符判定，所以它自己在多大输入下都是微秒级——用它来
    判断「估算值不值得挪进线程」不会把省下的时间又花掉。
    """
    return sum(len(message.content) + len(message.thinking or "") for message in messages)


async def _estimate_messages_tokens_off_loop(messages: list[ChatMessage]) -> int:
    """按字符量决定：同步估算，还是放进线程后 await。

    两种路径的返回值必须逐位相同（同一个函数，只是调度方式不同），否则「历史长短」
    会悄悄影响预算判定。锚点路径传入的是 `messages[index:]` 切片，阈值因此按「这次
    真正要估算的那段」算，而不是整份历史。
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
    """解析本轮的上下文预算；无法判定时返回 `None`。

    `model_limits` 允许调用方传入**已经解析好的** `(context_window, max_output_tokens)`：
    对话路径在 `resolve_chat_model` 里已经查过一次（`ChatModelSnapshot`），再按名字查
    一遍等于同一个请求把库扫两遍，而且两次读之间管理员改配置还会让「预算按 A 算、
    实际用 B 跑」。传 None 表示「我这里没有，请自己查」——CLI 与直接调用走这条路。

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

    两条路径的估算都经过 `_estimate_messages_tokens_off_loop`：长历史的逐字符循环
    不能让本请求之外的 SSE 陪着等。系统提示词那部分固定开销保持同步——它是常量级
    文本，量级与一个请求参数的线程往返相当，挪走没有收益。
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

    # 系统提示词 + 工具 schema 也要占窗口，且它们是每轮都发的固定开销。它算不算在
    # `used_tokens` 里由下面两条路径决定，不在这里一刀切地扣掉（见 build_budget）。
    overhead_tokens = estimate_tokens(render_system_prompt()) + TOOL_SCHEMA_OVERHEAD_TOKENS

    anchor = usable_anchor(messages, anchor)
    if anchor is not None:
        # 锚点来自客户端，不能无条件相信：声称覆盖 N 条消息、却报出一个远小于前缀
        # 真实规模的 token 数，就是拿它绕过上下文预检（I-08）。用前缀估算做一次核对，
        # 明显低于估算就丢掉锚点、退回全量估算（攻击代价 = 一次全量估算）。
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


#: 已告警过的「网关没报窗口」的模型 id。按 id 去重：这类告警每个请求都会触发，
#: 不去重会把日志刷满，反而盖住真正的问题（与 `chat_service._warn_unknown_once` 同理）。
#:
#: **必须是有界的**：key 来自 `model_name`，而网关列表不可用时任意字符串都会被
#: 放行（`model_service.get_known_model_ids` 返回 None 的分支），长度上限是 300 字符。
#: 用 set 会让单个账号把进程内存持续灌大——它永不清空。这里用固定容量的 dict 当
#: LRU：重复告警的价值很低，丢掉最旧的记录只会让一个已在告警的模型再告一次。
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
