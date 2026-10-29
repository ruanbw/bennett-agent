"""本轮的真实 token 用量与实测计时：从上游响应/流时序里取，不估算。
`context_tokens` 取最后一次请求的 prompt 数（`RunUsage` 是整轮累加）；量不到就整块留空，绝不补 0。
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
    #: 第一个输出分片到达的时刻（首字）。`None` = 这一步一个分片都没测到。
    first_chunk_at: float | None = None
    #: 最后一个输出分片到达的时刻。
    last_chunk_at: float | None = None
    #: 收到的输出分片个数。1 个 = 整段一次性到达，不能用来算解码速度（见 `_decode`）。
    chunks: int = 0
    #: 这一步的响应收尾时刻（`PartEndEvent` 关闭首 part 时）；没关闭过就留 `None`。
    closed_at: float | None = None


class TurnTimer:
    """在流里给一轮生成打点，收尾时折算成 `UsageTiming`（毫秒）。
    消息上的 `timestamp` 是请求开始/结束时刻而非分片时刻，拿它算首字延迟与解码速度会错位。
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
        """开始一次新的尝试：清掉上一步/工具的明细，整轮起点保留。
        必须清：`run_result` 只覆盖最终成功那次尝试，留着失败请求的耗时会变成「模型很慢」。
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
            # 每个 `ModelResponse` 的 part 从 index 0 开始：它是「新一步」的唯一信号。
            if event.index == 0:
                # 必须先关闭上一步再读锚点：读到旧锚点会把上一步的耗时重复算进这一步。
                self._close_step(now)
                self._current = _StepWindow(started_at=self._anchor)
            if self._current is not None and _has_output(event.part):
                # 首个分片常常直接躺在 `PartStartEvent` 里（不走 delta），漏掉它 TTFT 会多一整段。
                self._mark_chunk(now)
            return

        if isinstance(event, PartDeltaEvent):
            if self._current is not None:
                self._mark_chunk(now)
            return

        if isinstance(event, PartEndEvent):
            # `next_part_kind is None` 才是真收尾；不能只看 `index`：先 thinking(0) 再正文(1)
            # 时关闭事件落在 index 1，那样会把这一步一直挂着。
            if event.next_part_kind is None:
                # 这一条响应写完了：再往后的事件（工具调用/返回）不属于模型耗时。
                self._close_step(now)
            return

        # 用基类判断：结构化输出工具（`OutputToolCallEvent`）是子类，只认子类会让工具耗时少算。
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
            # 工具返回到下一步请求发出之间的等待不属于模型耗时，所以它成为下一步的锚点。
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
        `responses` 必须是本轮这一次尝试的 `new_messages()`：传整段历史会把以前几轮算进本轮。
        """
        now = self._clock()
        # 流结束时最后一步可能没被 `PartEndEvent` 关闭：用此刻收尾，否则连耗时一起丢掉。
        self._close_step(now)

        steps = len(responses)
        if steps <= 0:
            # 一个响应都没有 = 一次请求都没成功；报 `0 步` 会被读成「模型没干活」，整块不下发。
            logger.debug("本轮没有任何模型响应，不报计时")
            return None
        if not self._steps:
            # 响应在但流上没观测到分片：只剩 `turn_ms` 会像是模型慢，整块留空比半块好。
            logger.debug("流里未观测到任何分片，不报计时")
            return None

        turn_ms = _to_ms(now - self._turn_start)
        llm_ms = _to_ms(sum(_llm_seconds(step) for step in self._steps))
        tool_ms = _to_ms(self._tool_seconds)

        # 步数与响应对得上才按下标取 token；对不上就一起留空，不猜（见 `_decode`）。
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
    一个分片都没测到（网关回整段但不给流）时退回 `closed_at`：那一步确实占了时间。
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
    只算至少两个分片的步：单分片等于整段一次性到达，拿它当分母会算出无穷 tok/s。
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
    """秒 → 毫秒（四舍五入）：浮点秒进 JSON 会带一串尾数（`1.2000000000000002`）。"""
    return int(round(max(0.0, seconds) * 1000))
