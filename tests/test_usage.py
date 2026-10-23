"""真实用量与计时明细的提取口径。

用量这块的价值全在「取对那一个数字、拿不到就不报」两条上：一轮里如果有工具调用会有
多次模型请求，而每次都要重发一遍历史，所以只有**最后一次**请求的 `input_tokens` 等于
当前上下文长度；累加值（`RunUsage.input_tokens`）会把上下文算大。

计时那块的价值在「分母取对」：`tok/s` 的分母只能是真正的解码窗口，拿整轮墙钟当分母
会把工具执行算成「生成速度」。两条口径都用假时钟锁成确定性断言。
"""

import pytest
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage

from app.services.usage import TurnTimer, extract_turn_usage


def response(input_tokens=100, output_tokens=5, cache_read_tokens=0):
    return ModelResponse(
        parts=[TextPart(content="答")],
        usage=RequestUsage(input_tokens=input_tokens, output_tokens=output_tokens, cache_read_tokens=cache_read_tokens),
    )


def request(content="问"):
    return ModelRequest(parts=[UserPromptPart(content=content)])


def test_takes_the_last_request_as_the_context_length():
    """工具轮会重发历史：上下文长度只能是最后一次请求，不是累加。"""
    messages = [
        request(),
        ModelResponse(
            parts=[ToolCallPart(tool_name="t", args={})], usage=RequestUsage(input_tokens=1_000, output_tokens=20)
        ),
        ModelResponse(parts=[TextPart(content="答")], usage=RequestUsage(input_tokens=1_400, output_tokens=30)),
    ]

    usage = extract_turn_usage(messages)

    assert usage is not None
    assert usage.context_tokens == 1_400  # 不是 2400
    assert usage.output_tokens == 50  # 本轮总输出：两次请求之和
    assert usage.cache_read_tokens == 0


def test_cache_read_is_carried_as_a_subset():
    usage = extract_turn_usage([response(input_tokens=1_066, cache_read_tokens=896)])

    assert usage is not None
    assert usage.context_tokens == 1_066
    assert usage.cache_read_tokens == 896


def test_ignores_non_model_messages():
    """历史里还有 ModelRequest；它们没有 usage，不能被当成响应。"""
    usage = extract_turn_usage([request(), request(), response(input_tokens=77)])

    assert usage is not None and usage.context_tokens == 77


@pytest.mark.parametrize(
    "messages",
    [
        None,
        [],
        [request()],  # 只有请求：上游没走到响应
    ],
)
def test_missing_usage_is_reported_as_nothing(messages):
    """没有响应就没有用量。报 0 会被读成「上下文是空的」，比不报更糟。"""
    assert extract_turn_usage(messages) is None


def test_upstream_that_does_not_report_usage_is_skipped():
    """部分网关在流式模式下不回 usage（`input_tokens` 为 0）→ 不发这个事件。"""
    assert extract_turn_usage([request(), response(input_tokens=0)]) is None


def test_negative_or_missing_fields_do_not_crash():
    """字段缺失/异常时按「不可用」处理，而不是抛异常打断整轮收尾。"""
    bare = ModelResponse(parts=[TextPart(content="答")])
    bare.usage.input_tokens = -1  # type: ignore[misc]

    assert extract_turn_usage([bare]) is None


# ---------------------------------------------------------------------
# 计时明细（`TurnTimer`）
#
# 这块的价值全在「分母取对」与「量不到就不报」两条上。用假时钟写死时间点，
# 断言的是口径本身，而不是「大概跑了几百毫秒」——后者在 CI 上必然抖动。
# ---------------------------------------------------------------------
class FakeClock:
    """可手动推进的单调时钟（单测里把任意时间点写成确定值）。"""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


def text_start(index=0, content="首"):
    return PartStartEvent(index=index, part=TextPart(content=content))


def text_delta(index=0, content="续"):
    return PartDeltaEvent(index=index, delta=TextPartDelta(content_delta=content))


def text_end(index=0, content="终"):
    return PartEndEvent(index=index, part=TextPart(content=content), next_part_kind=None)


def tool_call(call_id="c1"):
    return FunctionToolCallEvent(ToolCallPart(tool_name="t", args={"x": 1}, tool_call_id=call_id))


def tool_result(call_id="c1"):
    return FunctionToolResultEvent(ToolReturnPart(tool_name="t", content="ok", tool_call_id=call_id))


def _drive(timer, clock, script):
    """按 (推进秒数, 事件) 的顺序喂事件；推进发生在喂事件**之前**。"""
    for advance, event in script:
        clock.advance(advance)
        timer.observe(event)


def test_timing_measures_one_plain_turn():
    """两步都测得到：首字 0.1s、解码 0.2s、步数、整轮墙钟。"""
    clock = FakeClock()
    timer = TurnTimer(clock=clock)
    timer.begin_attempt()

    _drive(
        timer,
        clock,
        [
            (0.10, text_start(content="你")),  # 首字
            (0.20, text_delta(content="好")),  # 解码结束（第 2 个分片）
            (0.05, text_end()),
        ],
    )
    timing = timer.finish([response(output_tokens=7)])

    assert timing is not None
    assert timing.steps == 1
    assert timing.turn_ms == 350
    # llm_ms = 请求发出 → 最后一个分片 = 0.1 + 0.2
    assert timing.llm_ms == 300
    assert timing.tool_ms == 0
    assert timing.ttft_ms == 100
    # 解码窗口 = 首字 → 末字 = 0.2s，token 数取自该步的响应
    assert (timing.decode_ms, timing.decode_tokens) == (200, 7)


def test_timing_attributes_tool_time_separately_from_llm_time():
    """工具那 0.4s 必须进 `tool_ms`，**不能**进 `llm_ms`。

    这条是整块计时最容易被写错的地方：把工具执行算进模型耗时，慢的是工具却会
    显示成「模型很慢」，用户据此去换模型——换了也没用。
    """
    clock = FakeClock()
    timer = TurnTimer(clock=clock)
    timer.begin_attempt()

    _drive(
        timer,
        clock,
        [
            (0.10, text_start(content="我查一下")),
            (0.05, text_end()),
            (0.00, tool_call()),
            (0.40, tool_result()),  # 工具执行 0.4s
            (0.10, text_start(content="答")),  # 第二步首字
            (0.20, text_delta(content="案")),
            (0.05, text_end()),
        ],
    )
    timing = timer.finish([response(output_tokens=3), response(output_tokens=2)])

    assert timing is not None
    assert timing.steps == 2
    assert timing.tool_ms == 400
    # llm_ms 到**最后一个分片**为止：第一步 0.1，第二步 0.1 首字 + 0.2 解码 = 0.3。
    # 收尾那个没有内容的分片（text_end 在 0.05 之后）不算进模型耗时。
    assert timing.llm_ms == 400
    assert timing.ttft_ms == 100  # 两步首字都是 0.1s
    # 第一步只有 1 个分片（不算解码样本），第二步窗口 0.2s / 2 tok
    assert (timing.decode_ms, timing.decode_tokens) == (200, 2)


def test_turn_ms_is_wall_clock_including_tool_time():
    """`turn_ms` 是用户实际等了多久：工具执行算在内，与 `llm_ms` 口径不同。"""
    clock = FakeClock()
    timer = TurnTimer(clock=clock)
    timer.begin_attempt()

    _drive(
        timer,
        clock,
        [
            (0.10, text_start()),
            (0.05, text_end()),
            (0.00, tool_call()),
            (0.40, tool_result()),
            (0.10, text_start()),
            (0.05, text_end()),
        ],
    )
    timing = timer.finish([response(), response()])

    assert timing is not None
    assert timing.turn_ms == 700  # 0.1 + 0.05 + 0.4 + 0.1 + 0.05
    # 每步只有一个分片，模型耗时取该步收尾：第一步 0.1，第二步 0.1
    assert timing.llm_ms == 200
    assert timing.tool_ms + timing.llm_ms < timing.turn_ms


def test_single_chunk_step_is_not_used_as_a_decode_sample():
    """整段一次性到达（只有 1 个分片）时不能算解码速度：那个分母约等于 0。

    工具调用那一轮就是这种形状（入参一次给全）。把它算进去，速度会变成
    几万 tok/s——不是「快」，是没有测到。
    """
    clock = FakeClock()
    timer = TurnTimer(clock=clock)
    timer.begin_attempt()

    _drive(
        timer,
        clock,
        [
            (0.10, text_start(content="整段输出一次到齐")),  # 唯一的分片
            (0.50, text_end()),
        ],
    )
    timing = timer.finish([response(output_tokens=42)])

    assert timing is not None
    assert timing.steps == 1
    # 首字仍测得准（就是这个分片）
    assert timing.ttft_ms == 100
    # 没有至少两个分片 → 解码窗口不可信，整对留空，而不是给一个无穷大的速度
    assert (timing.decode_ms, timing.decode_tokens) == (None, None)


def test_timing_is_none_when_no_response_arrived():
    """一次模型请求都没成功时整块不给。`0 步` 会被读成「模型没干活」。"""
    clock = FakeClock()
    timer = TurnTimer(clock=clock)
    timer.begin_attempt()
    _drive(timer, clock, [(0.10, text_start())])

    assert timer.finish([]) is None


def test_timing_is_none_when_no_chunks_were_observed():
    """响应在、但流里一个分片都没经过我们：半块数字解释不了任何事，整块留空。"""
    clock = FakeClock()
    timer = TurnTimer(clock=clock)
    timer.begin_attempt()
    clock.advance(0.5)

    assert timer.finish([response()]) is None


def test_response_step_misalignment_drops_decoded_numbers_only():
    """步数与响应数对不上时，只丢需要对齐的那几个数。

    `llm_ms`/`turn_ms`/`tool_ms` 来自流里的锚点，不依赖对齐，仍然可信；
    解码 token 要按下标取响应，对不上就**不能猜**——猜错会把别人的 token 数
    配到这一步的窗口上，算出一个错得很离谱的速度。
    """
    clock = FakeClock()
    timer = TurnTimer(clock=clock)
    timer.begin_attempt()

    _drive(
        timer,
        clock,
        [
            (0.10, text_start()),
            (0.20, text_delta()),
            (0.05, text_end()),
        ],
    )
    timing = timer.finish([response(output_tokens=5), response(output_tokens=5)])

    assert timing is not None
    assert timing.steps == 2  # 步数以响应为准
    assert timing.llm_ms == 300
    assert (timing.decode_ms, timing.decode_tokens) == (None, None)
    assert timing.ttft_ms is None


def test_retry_attempt_resets_steps_but_keeps_wall_clock():
    """重试：步数只算最终成功那次尝试，`turn_ms` 仍含退避等待。

    `run_result` 只覆盖成功的那次尝试，所以它的响应序列里没有失败尝试的请求。
    不重置步数，就会出现「2 步却有 1 条响应」的错位；不保留墙钟，用户实际
    等了 1.5s 会被报成 0.5s。
    """
    clock = FakeClock()
    timer = TurnTimer(clock=clock)
    timer.begin_attempt()

    _drive(timer, clock, [(0.10, text_start()), (0.05, text_end())])  # 第一次尝试失败
    clock.advance(1.0)  # 重试退避
    timer.begin_attempt()  # 第二次尝试
    _drive(timer, clock, [(0.10, text_start()), (0.20, text_delta()), (0.05, text_end())])

    timing = timer.finish([response(output_tokens=6)])

    assert timing is not None
    assert timing.steps == 1  # 不含失败尝试
    assert timing.turn_ms == 1500  # 0.15 + 1.0 退避 + 0.1 + 0.2 + 0.05
    assert timing.llm_ms == 300
    assert (timing.decode_ms, timing.decode_tokens) == (200, 6)


def test_timing_counts_tool_ms_as_a_real_zero_when_no_tool_ran():
    """没有工具时 `tool_ms` 是**真实的 0**，与「量不到」不是一回事。

    「量不到」的表示是整块 `timing` 为 None；块内的 0 是有意义的测量结果。
    """
    clock = FakeClock()
    timer = TurnTimer(clock=clock)
    timer.begin_attempt()
    _drive(timer, clock, [(0.10, text_start()), (0.20, text_delta()), (0.05, text_end())])

    timing = timer.finish([response()])

    assert timing is not None and timing.tool_ms == 0


def test_thinking_part_before_text_does_not_break_step_boundaries():
    """先思考后正文：一步里有两个 part，收尾必须落在真的最后一个 part 上。"""
    clock = FakeClock()
    timer = TurnTimer(clock=clock)
    timer.begin_attempt()

    _drive(
        timer,
        clock,
        [
            # thinking：index 0
            (0.10, PartStartEvent(index=0, part=ThinkingPart(content="想"))),
            (0.05, PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta="想"))),
            (0.00, PartEndEvent(index=0, part=ThinkingPart(content="想想"), next_part_kind="text")),
            # 正文：index 1，且是这一步的最后一个 part
            (0.10, PartStartEvent(index=1, part=TextPart(content="答"))),
            (0.20, PartDeltaEvent(index=1, delta=TextPartDelta(content_delta="案"))),
            (0.05, PartEndEvent(index=1, part=TextPart(content="答案"), next_part_kind=None)),
        ],
    )
    timing = timer.finish([response(output_tokens=4)])

    assert timing is not None
    assert timing.steps == 1
    # 首字是 thinking 的第一个分片（0.1s），模型占用一直到正文最后一个分片（0.45s）
    assert timing.ttft_ms == 100
    assert timing.llm_ms == 450  # 0.1 + 0.05 + 0.1 + 0.2
    assert timing.tool_ms == 0
    assert (timing.decode_ms, timing.decode_tokens) == (350, 4)  # 首分片 → 末分片


def test_parallel_tool_calls_are_summed_not_wall_clock():
    """并发工具的耗时是各自之和（可能大于墙钟那一段），这是刻意的口径。"""
    clock = FakeClock()
    timer = TurnTimer(clock=clock)
    timer.begin_attempt()

    _drive(
        timer,
        clock,
        [
            (0.10, text_start()),
            (0.00, text_end()),
            (0.00, tool_call("c1")),
            (0.10, tool_call("c2")),  # 两个工具并发
            (0.30, tool_result("c1")),  # c1 用时 0.4s
            (0.00, tool_result("c2")),  # c2 用时 0.4s
            (0.10, text_start()),
            (0.10, text_delta()),
            (0.05, text_end()),
        ],
    )
    timing = timer.finish([response(), response()])

    assert timing is not None
    # c1：0.4s / c2：0.3s（c2 在 c1 之后 0.1s 才发出）。各自耗时之和，不是墙钟那 0.3s。
    assert timing.tool_ms == 700


# ---------------------------------------------------------------------
# 与真实 pydantic-ai 流的对接
#
# 上面的用例全用手工构造的事件，锁的是口径；这一条锁**假设**——「index 0 的
# PartStartEvent 开启一步」「PartEndEvent 且 next_part_kind 为 None 关闭这一步」
# 这两条是照着 2.31.1 的实测事件顺序写的，升级 pydantic-ai 后若顺序变了，
# 计时会安静地变成一堆 0 或整块 None，只有跑这条才会发现。
# ---------------------------------------------------------------------
async def test_timer_measures_a_real_two_step_run_with_a_tool():
    """真跑一次「调工具 → 再答」：步数、工具耗时、模型耗时都必须对得上。"""
    import asyncio

    from pydantic_ai import Agent
    from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
    from pydantic_ai.tools import Tool

    calls = {"n": 0}

    async def stream_fn(messages, info: AgentInfo):
        calls["n"] += 1
        if calls["n"] == 1:
            yield DeltaToolCalls({0: DeltaToolCall(name="echo", json_args='{"x": 1}', tool_call_id="c1")})
        else:
            yield "答"
            await asyncio.sleep(0.05)
            yield "案"

    async def echo(x: int) -> str:
        await asyncio.sleep(0.12)
        return f"ok {x}"

    agent = Agent(FunctionModel(stream_function=stream_fn), tools=[Tool(echo)])

    timer = TurnTimer()
    timer.begin_attempt()
    async with agent.run_stream_events("问") as stream:
        async for event in stream:
            if type(event).__name__ != "AgentRunResultEvent":
                timer.observe(event)
            else:
                run_result = event.result

    responses = [message for message in run_result.new_messages() if isinstance(message, ModelResponse)]
    timing = timer.finish(responses)

    assert timing is not None
    assert timing.steps == 2
    # 工具真的睡了 0.12s：它必须出现在 tool_ms 里，而不是 llm_ms
    assert timing.tool_ms >= 100
    # 第一步是一次性给出的工具入参（单分片，不算解码样本）；
    # 第二步是「答」+ 0.05s + 「案」，所以解码窗口约为 50ms、token 数取自第二步。
    assert timing.decode_ms is not None and timing.decode_ms >= 30
    assert timing.decode_tokens is not None and timing.decode_tokens > 0
    # 两个数一起给：前端相除才是速度
    assert (timing.decode_ms is None) == (timing.decode_tokens is None)
    # 模型耗时不该把工具那 0.12s 算进来
    assert timing.llm_ms < timing.turn_ms
    assert timing.tool_ms + timing.llm_ms <= timing.turn_ms + 50
    # 两步都测到了首字
    assert timing.ttft_ms is not None and timing.ttft_ms >= 0
