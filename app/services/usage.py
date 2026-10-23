"""本轮生成的**真实** token 用量与**实测**计时：从上游响应/流时序里取，不估算。

上游每次响应都带 usage（OpenAI 兼容的 `prompt_tokens` / `completion_tokens`），
pydantic-ai 把它记在每条 `ModelResponse.usage` 上，并在 `run_stream_events` 的
**末尾**以一个 `AgentRunResultEvent` 交回运行结果——也就是说这份数据一直在本项目
消费的那条流里，只是此前被丢掉了（`translate_event` 还把它当成「未识别的
pydantic-ai 流事件」打了 WARNING）。真实值比字符数、比 `token_budget` 的估算都准，
所以只要拿得到就用它。

两个容易搞错的点，都在 `extract_turn_usage` 里定死：

1. **取最后一次请求的 `input_tokens`，不是整轮的累加。** 一轮里如果有工具调用，
   会有多次模型请求，每次都必须重发一遍历史，`RunUsage.input_tokens` 是这些请求的
   **总和**（例如 1066 + 1400 = 2466）。真正等于「当前上下文长度」的只有最后一次
   请求的 prompt 数。
2. **拿不到就不报。** 部分网关在流式模式下不回 usage（`input_tokens` 为 0），此时
   返回 `None` 由调用方跳过这个事件——报一个 0 会被读成「上下文是空的」，比不报更糟。

## 计时（`TurnTimer`）

用量是「多少」，计时是「多快」。后者必须**在流里量**：`extract_turn_usage` 只拿到
消息序列，消息上的 `timestamp` 是「响应开始/结束」的时刻而非分片时刻，用它算首字
延迟与解码速度会得到一个看似合理、实则错位的数。所以计时由 `TurnTimer` 在
`stream_chat_events` 消费事件的同一循环里打点（见 `app/services/chat_service.py`），
本模块只负责「拿到事件 → 更新锚点 → 收尾时折算成毫秒」。

三条口径在这里定死，改动时不要放松：

1. **解码速度的分母只能是解码窗口。** 一轮里如果有工具调用，墙钟时间包含工具执行、
   上游排队与重试退避。拿它当分母，一个跑了 20 秒工具的一轮会被算成 3 tok/s——
   那不是模型慢，是工具慢，用户会据此去换模型。解码窗口 = 该步「第一个输出分片 →
   最后一个输出分片」，且**必须真的出现过增量分片**（`PartDeltaEvent`）才算测到：
   网关一次性返回整段文本时窗口只有一个瞬间，算出来的速度是无穷大，那比不给更糟。
2. **量不到就整块不给（`None`），绝不补 0。** `0 tok/s` 会被读成「模型卡住了」，
   `0 步` 会被读成「一个请求都没发」。整块 `timing` 缺席时前端只是不显示这些统计项。
   唯一的「真 0」是 `tool_ms`：一轮里没调用工具时它确实是 0，这与「量不到」不同
   ——量不到时整块都不下发。
3. **响应与步要能对齐才敢报解码 token。** 解码 token 数取自对应 `ModelResponse.usage`，
   所以收尾时用 `new_messages()` 里本轮的响应序列按下标对齐；数目对不上（结构变了 /
   调用方传了历史）就把 `decode_ms` 与 `decode_tokens` 一起留空，而不是猜一个。
   注意必须是 `new_messages()` 而不是 `all_messages()`：后者含客户端回放的历史，
   拿来数步数会把**以前几轮**的请求也算进本轮，与「37 步」的真实语义不符。
"""

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    ToolCallEvent,
    ToolResultEvent,
)

from app.schemas.events import UsageTiming

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnUsage:
    """一轮生成的真实用量。三个数字都来自上游，未做任何换算。"""

    #: 本轮最后一次请求的 prompt token 数 = 当前上下文占用的长度（含缓存命中部分）
    context_tokens: int
    #: 本轮所有请求的输出 token 之和（工具调用间隙生成的内容也算）
    output_tokens: int
    #: 上下文里命中上游提示缓存的部分（`context_tokens` 的子集）
    cache_read_tokens: int


def extract_turn_usage(messages: list[ModelMessage] | None) -> TurnUsage | None:
    """从一轮运行的消息序列里取真实用量；拿不到（或上游没报）返回 `None`。"""
    responses = [message for message in messages or [] if isinstance(message, ModelResponse)]
    if not responses:
        return None

    last_usage = responses[-1].usage
    context_tokens = int(getattr(last_usage, "input_tokens", 0) or 0)
    if context_tokens <= 0:
        # 上游没在流式响应里回 usage。宁可让这个事件缺席，也不要给一个 0。
        logger.debug("上游未返回 usage（最后一次请求的 input_tokens 为 0），本轮不报用量")
        return None

    return TurnUsage(
        context_tokens=context_tokens,
        output_tokens=sum(int(getattr(message.usage, "output_tokens", 0) or 0) for message in responses),
        cache_read_tokens=int(getattr(last_usage, "cache_read_tokens", 0) or 0),
    )


@dataclass
class _StepWindow:
    """一步（一次模型请求）的计时锚点。全部是单调时钟读数。"""

    #: 这一步的请求「可以发出」的时刻：上一步结束（或无工具交接时上一步收尾）之后。
    #: 首步的锚点是整轮起点。用它当分母起点，模型排队与首字等待才算进模型耗时。
    started_at: float
    #: 第一个**输出**分刻到达的时刻（首字）。`None` = 这一步一个分片都没测到。
    first_chunk_at: float | None = None
    #: 最后一个输出分片到达的时刻。
    last_chunk_at: float | None = None
    #: 收到的输出分片个数。1 个 = 整段一次性到达，不能用来算解码速度（见 `_decode`）。
    chunks: int = 0
    #: 这一步的响应收尾时刻（`PartEndEvent` 关闭首 part 时）；没关闭过就留 `None`。
    closed_at: float | None = None


class TurnTimer:
    """在**流里**给一轮生成打点，收尾时折算成 `UsageTiming`（毫秒）。

    为什么不在 `extract_turn_usage` 里一次算完：消息上的 `timestamp` 是「这次请求
    开始/结束」的时刻，不是分片时刻。拿它算首字延迟与解码速度，会得到一个看起来
    合理、实际错位的数——比不报更糟，因为它不会让人怀疑口径。

    用法（`stream_chat_events` 里就是一个 `observe`）：

    ```python
    timer = TurnTimer()                     # 记下整轮起点
    timer.begin_attempt()                   # 每次尝试（含重试）开始前重置步/工具明细
    async for event in stream:
        timer.observe(event)                # 打点；不产生任何事件
    timing = timer.finish(responses)        # 收尾：量不到就返回 None
    ```

    时间源可注入（`clock`）：单测里给一个假时钟就能把「0.15s 首字 + 0.2s 解码」
    写成确定性的断言。默认 `time.monotonic`，与 `loop.time()` 同源，可以和
    `stream_started_at` 换算。
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._turn_start = clock()
        #: 上一步收尾 / 最后一次工具返回的时刻 = 下一步请求「可以发出」的时刻。
        self._anchor = self._turn_start
        self._steps: list[_StepWindow] = []
        self._current: _StepWindow | None = None
        #: 正在执行的工具：`tool_call_id` → 开始时刻。返回时弹出并累加。
        self._running_tools: dict[str, float] = {}
        self._tool_seconds = 0.0

    # -- 打点 ---------------------------------------------------------
    def begin_attempt(self) -> None:
        """开始一次新的尝试：清掉上一步/工具的明细，整轮起点**保留**。

        为什么重试要清：`run_result` 只覆盖**最终成功**的那次尝试，它的响应序列
        里没有失败尝试的请求。不清的话步数会与响应对不上（解码 token 只能整块留空），
        已经计入的失败请求耗时会变成一笔谁也解释不了的「模型很慢」。`turn_ms`
        仍然含重试退避，因为它量的是用户实际等了多久——两者口径不同，这是刻意的。
        """
        self._steps = []
        self._current = None
        self._running_tools = {}
        self._tool_seconds = 0.0
        self._anchor = self._clock()

    def observe(self, event: object) -> None:
        """吃一个 pydantic-ai 流事件，只记账，不改事件。"""
        now = self._clock()

        if isinstance(event, PartStartEvent):
            # 每个 `ModelResponse` 的 part 从 index 0 开始：index 0 的起始 part
            # 就是「新一步」的唯一可靠信号（流上没有单独的「请求开始」事件）。
            if event.index == 0:
                # 先关闭上一步，再读锚点，顺序不能反：
                #
                # - 首步：此刻没有「上一步」，`_close_step` 是空操作，锚点仍是整轮
                #   起点（或最后一次工具返回的时刻）——这正是这一步的请求发出时刻。
                # - 后续步：上一步正常收尾时锚点**已经**是它的收尾时刻（`_close_step`
                #   在 `PartEndEvent` 就推过了），再调一次仍是空操作，周期间隙保留。
                # - 上一步没收到收尾事件（上游提前断开）时，这一步的起点只能取此刻：
                #   读到的是上一次收尾的旧值，会把上一步整段耗时重复算进这一步。
                self._close_step(now)
                self._current = _StepWindow(started_at=self._anchor)
            if self._current is not None and _has_output(event.part):
                # 首个分片常常直接躺在 `PartStartEvent` 里（不做 delta），
                # 漏掉它会把首字延迟算多整整一段，也会把解码窗口算短。
                self._mark_chunk(now)
            return

        if isinstance(event, PartDeltaEvent):
            if self._current is not None:
                self._mark_chunk(now)
            return

        if isinstance(event, PartEndEvent):
            # `next_part_kind is None` = 这条响应没有后续 part 了，是真收尾。
            # 不看 `index`：一步可能先出 thinking（index 0）再出正文（index 1），
            # 那种形状下关闭事件落在 index 1 上，只认 index 0 会把这一步一直挂着。
            if event.next_part_kind is None:
                # 这一条响应写完了：再往后的事件（工具调用/返回）不属于模型耗时。
                self._close_step(now)
            return

        # 用基类判断：函数工具与结构化输出工具（`OutputToolCallEvent`）都是
        # `ToolCallEvent`/`ToolResultEvent` 的子类，按子类判断会漏掉兄弟类，
        # 表现为工具耗时悄悄少算——正是这条链最不该出错的地方。
        if isinstance(event, ToolCallEvent):
            call_id = getattr(event.part, "tool_call_id", "") or ""
            if call_id:
                self._running_tools[call_id] = now
            return

        if isinstance(event, ToolResultEvent):
            call_id = getattr(event.part, "tool_call_id", "") or ""
            started_at = self._running_tools.pop(call_id, None)
            if started_at is not None:
                self._tool_seconds += max(0.0, now - started_at)
            # 工具返回后下一步请求才发得出去 → 它成为下一步的锚点。
            # 不这么做，会把这 0.3s 的等待算进下一步的 `llm_ms`。
            self._anchor = now
            return

    # -- 内部 ---------------------------------------------------------
    def _mark_chunk(self, now: float) -> None:
        step = self._current
        if step is None:  # pragma: no cover - 理论上不会（PartStart index 0 先到）
            return
        if step.first_chunk_at is None:
            step.first_chunk_at = now
        step.last_chunk_at = now
        step.chunks += 1

    def _close_step(self, now: float) -> None:
        step = self._current
        if step is None:
            return
        step.closed_at = now
        self._steps.append(step)
        self._current = None
        self._anchor = now

    # -- 收尾 ---------------------------------------------------------
    def finish(self, responses: Sequence[ModelResponse]) -> UsageTiming | None:
        """把打点折算成 `UsageTiming`；一个数都量不到时返回 `None`。

        `responses` 必须是**本轮这一次尝试**的响应序列（`AgentRunResult.new_messages()`
        里的 `ModelResponse`），用来数步数与取解码 token 数。传整段会话历史会
        把以前几轮的请求也算进「本轮几步」。
        """
        now = self._clock()
        # 流结束时最后一步可能还没被 `PartEndEvent` 关闭（上游提前断开、
        # 或最后一条 part 没给收尾事件）：用此刻收尾，否则那一步会连耗时一起丢掉。
        self._close_step(now)

        steps = len(responses)
        if steps <= 0:
            # 一个响应都没有 = 一次模型请求都没成功。此时 `0 步` 会被读成「模型
            # 没干活」，而真实原因是上游没回任何响应。整块不下发。
            logger.debug("本轮没有任何模型响应，不报计时")
            return None
        if not self._steps:
            # 响应在，但流上一个分片都没经过我们（例如调用方在别的链路消费了
            # 事件）。这时 `llm_ms`/`ttft_ms` 全是空的，报一个 `turn_ms` 也解释
            # 不了任何事，整块留空比给半块更不容易被误读。
            logger.debug("流里未观测到任何分片，不报计时")
            return None

        turn_ms = _to_ms(now - self._turn_start)
        llm_ms = _to_ms(sum(_llm_seconds(step) for step in self._steps))
        tool_ms = _to_ms(self._tool_seconds)

        # 步数对得上才敢按下标把「响应」与「步」对应起来取 token 数；对不上时
        # 解码 token 与首字均值一起留空，而不是猜一个（见模块 docstring 第 3 条）。
        aligned = len(self._steps) == steps
        ttft_ms = _average_ttft(self._steps) if aligned else None
        decode_ms, decode_tokens = _decode(self._steps, responses) if aligned else (None, None)

        return UsageTiming(
            steps=steps,
            turn_ms=turn_ms,
            llm_ms=llm_ms,
            tool_ms=tool_ms,
            ttft_ms=ttft_ms,
            decode_ms=decode_ms,
            decode_tokens=decode_tokens,
        )


def _has_output(part: object) -> bool:
    """这个 part 是否已经带了输出内容（用于识别「首个分片就在起始 part 里」）。"""
    content = getattr(part, "content", None)
    if isinstance(content, str):
        if content:
            return True
    elif content:
        return True
    # `ToolCallPart` 的输出是入参：`args` 为空串/dict 时还没产出任何 token。
    args = getattr(part, "args", None)
    return bool(args)


def _llm_seconds(step: _StepWindow) -> float:
    """一步的模型请求占用：从「请求可以发出」到该步最后一个输出分片。

    分片一个都没测到（网关直接回整段但不给流）时退回 `closed_at`：那一步确实
    占了时间，只是我们没看见分片边界。
    """
    end = step.last_chunk_at if step.last_chunk_at is not None else step.closed_at
    if end is None:  # pragma: no cover - 未关闭的步在 `finish` 里已被关闭
        return 0.0
    return max(0.0, end - step.started_at)


def _average_ttft(steps: Sequence[_StepWindow]) -> int | None:
    """各步首字延迟的平均值（毫秒）；一步都没测到就 `None`。"""
    samples = [step.first_chunk_at - step.started_at for step in steps if step.first_chunk_at is not None]
    if not samples:
        return None
    return _to_ms(sum(samples) / len(samples))


def _decode(
    steps: Sequence[_StepWindow],
    responses: Sequence[ModelResponse],
) -> tuple[int | None, int | None]:
    """解码阶段的累计耗时与 token 数（成对；无有效样本时 `(None, None)`）。

    只有**至少两个分片**的步才计入：单分片意味着整段输出一次性到达，窗口
    约等于 0，用它当分母会算出成千上万的 tok/s——那不是「快」，是没有测到。
    工具调用那一步常常就是这种形状（入参一次给全），把它算进去会把速度抬高。

    两者也必须同时成立：窗口为正、token 为正。只有一个成立时给 `(None, None)`，
    因为「无穷 tok/s」与「0 tok/s」都是错误结论。
    """
    window_seconds = 0.0
    tokens = 0
    for step, response in zip(steps, responses, strict=False):
        if step.chunks < 2 or step.first_chunk_at is None or step.last_chunk_at is None:
            continue
        window = step.last_chunk_at - step.first_chunk_at
        if window <= 0:
            continue
        window_seconds += window
        tokens += int(getattr(response.usage, "output_tokens", 0) or 0)

    decode_ms = _to_ms(window_seconds)
    if decode_ms <= 0 or tokens <= 0:
        return None, None
    return decode_ms, tokens


def _to_ms(seconds: float) -> int:
    """秒 → 毫秒（四舍五入到整数）。

    协议层只用整毫秒：亚毫秒精度对「这轮用了 1.2 秒」没有任何意义，而浮点秒
    在 JSON 里会带一串尾数（`1.2000000000000002`），前端还要自己 round。
    """
    return int(round(max(0.0, seconds) * 1000))
