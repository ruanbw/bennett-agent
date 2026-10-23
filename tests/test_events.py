"""翻译层与序列化契约。

覆盖 R-1（类型化事件 + 单点序列化）与 R-2（`isinstance` 分派）。

翻译层是「pydantic-ai 说的事件」与「前端看得懂的事件」之间唯一的接缝。它此前用
`type(event).__name__` 字符串比较分派，升级改名后表现为「静默无输出」——没有任何
地方会报错。下面的用例既锁形状，也锁「不认识的事件会留下日志痕迹」。
"""

import json
import logging

import pydantic
import pytest
from pydantic_ai.messages import (
    FinalResultEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelResponse,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    RetryPromptPart,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolCallPartDelta,
    ToolReturnPart,
)
from pydantic_ai.run import AgentRunResultEvent

from app.api.chat import to_sse_frame
from app.schemas.events import (
    ClarificationRequest,
    ContentDelta,
    ContextCompacted,
    StreamError,
    ThinkingDelta,
    ToolCall,
    ToolResult,
    Usage,
    UsageTiming,
)
from app.services import chat_service
from app.services.chat_service import CLARIFICATION_TOOL_NAME, translate_event


@pytest.fixture(autouse=True)
def _reset_unknown_event_warnings():
    """未识别事件的告警按类型**全局**去重，不隔离就会跨用例互相吞掉日志。"""
    chat_service._WARNED_UNKNOWN_EVENT_KEYS.clear()
    yield
    chat_service._WARNED_UNKNOWN_EVENT_KEYS.clear()


def translate(event):
    return list(translate_event(event))


# ---------------------------------------------------------------------
# Part 事件
# ---------------------------------------------------------------------
def test_text_part_start_becomes_content_delta():
    events = translate(PartStartEvent(index=0, part=TextPart(content="你好")))

    assert events == [ContentDelta(delta="你好")]


def test_thinking_part_start_becomes_thinking_delta():
    events = translate(PartStartEvent(index=0, part=ThinkingPart(content="想了想")))

    assert events == [ThinkingDelta(delta="想了想")]


def test_empty_part_start_is_ignored():
    """空 part 不该产出事件，否则前端会收到一堆空增量。"""
    assert translate(PartStartEvent(index=0, part=TextPart(content=""))) == []
    assert translate(PartStartEvent(index=0, part=ThinkingPart(content=""))) == []


def test_text_part_delta_becomes_content_delta():
    events = translate(PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="增量")))

    assert events == [ContentDelta(delta="增量")]


def test_thinking_part_delta_becomes_thinking_delta():
    events = translate(PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta="边想")))

    assert events == [ThinkingDelta(delta="边想")]


def test_empty_deltas_are_ignored():
    assert translate(PartDeltaEvent(index=0, delta=TextPartDelta(content_delta=""))) == []
    assert translate(PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta=""))) == []


def test_tool_call_delta_is_intentionally_ignored(caplog):
    """工具入参增量有意忽略：入参在 FunctionToolCallEvent 到达时一次性给出。"""
    with caplog.at_level(logging.DEBUG, logger="app.services.chat_service"):
        events = translate(PartDeltaEvent(index=0, delta=ToolCallPartDelta(args_delta='{"q":')))

    assert events == []
    assert "ToolCallPartDelta" in caplog.text
    # 有意忽略的事件不能混进「未识别」的 WARNING，否则唯一的渲染停摆信号会被淹没
    assert [record.levelno for record in caplog.records if record.levelno >= logging.WARNING] == []


# ---------------------------------------------------------------------
# 工具事件
# ---------------------------------------------------------------------
def tool_call_event(tool_name, args, tool_call_id="call-1"):
    return FunctionToolCallEvent(part=ToolCallPart(tool_name=tool_name, args=args, tool_call_id=tool_call_id))


def test_function_tool_call_becomes_tool_call():
    events = translate(tool_call_event("tavily_search", {"query": "Python 3.13"}))

    assert len(events) == 1
    assert isinstance(events[0], ToolCall)
    assert events[0].tool_name == "tavily_search"
    assert events[0].tool_call_id == "call-1"
    # 入参以字符串形态交给前端，由展示层决定怎么格式化
    assert json.loads(events[0].args) == {"query": "Python 3.13"}


def test_tool_args_already_a_string_are_passed_through():
    events = translate(tool_call_event("tavily_search", '{"query":"x"}'))

    assert events[0].args == '{"query":"x"}'


def test_function_tool_result_uses_part_content():
    """必须取 part.content（工具结果本体），不是 event.content。

    `FunctionToolResultEvent.content` 是「结果之后追加给模型的额外内容」，
    语义完全不同；用错会静默改变前端展示。
    """
    event = FunctionToolResultEvent(
        part=ToolReturnPart(tool_name="tavily_search", content="搜索结果", tool_call_id="call-1"),
        content="本字段不是工具结果",
    )

    events = translate(event)

    assert isinstance(events[0], ToolResult)
    assert events[0].result == "搜索结果"
    assert events[0].tool_name == "tavily_search"
    assert events[0].outcome == "success"


def test_tool_result_carries_failed_outcome():
    """被隔离的工具异常必须保留 failed 信号，前端才不会画成绿色成功。"""
    event = FunctionToolResultEvent(
        part=ToolReturnPart(
            tool_name="tavily_search",
            content="工具执行失败（内部错误）",
            tool_call_id="call-2",
            outcome="failed",
        ),
    )

    out = translate(event)[0]

    assert isinstance(out, ToolResult)
    assert out.outcome == "failed"
    assert out.result == "工具执行失败（内部错误）"


def test_non_string_tool_result_is_serialised():
    event = FunctionToolResultEvent(
        part=ToolReturnPart(tool_name="t", content={"a": [1, 2]}, tool_call_id="c"),
    )

    assert json.loads(translate(event)[0].result) == {"a": [1, 2]}


def test_retry_prompt_result_is_translated():
    """工具报错走 RetryPromptPart，前端同样要能展示，并标成失败。"""
    event = FunctionToolResultEvent(
        part=RetryPromptPart(content="参数不合法", tool_name="t", tool_call_id="c"),
    )

    out = translate(event)[0]

    assert isinstance(out, ToolResult)
    assert out.outcome == "failed"


# ---------------------------------------------------------------------
# 意图确认提问（R-6 的接缝）
#
# `translate_event` 消费的是 `FunctionToolCallEvent`（工具**开始执行**），它比工具
# 返回早一步——服务端那时还不知道工具会拒绝。校验规则因此必须与工具**完全一致**：
# 模型传 600 字问题 → 工具返回「超限」错误 → 但弹窗已经发出去了 → 模型重试带新的
# tool_call_id → 去重拦不住 → 用户看到第二个弹窗。两边共用 `normalize_clarification`。
# ---------------------------------------------------------------------
def clarification_call(args, tool_call_id="ask-1"):
    return tool_call_event(CLARIFICATION_TOOL_NAME, args, tool_call_id)


def test_clarification_becomes_typed_event_not_tool_call():
    """提问必须是一等公民事件，前端不再比对 tool_name 字符串。"""
    events = translate(
        clarification_call(
            {
                "question": "生成 React 还是 Vue？",
                "options": ["React", "Vue"],
                "allow_custom_input": True,
            },
            "ask-1",
        )
    )

    assert len(events) == 1
    request = events[0]
    assert isinstance(request, ClarificationRequest)
    assert request.tool_call_id == "ask-1"
    assert request.question == "生成 React 还是 Vue？"
    assert request.options == ["React", "Vue"]
    assert request.allow_custom_input is True
    assert request.type == "clarification"


def test_clarification_accepts_json_string_args():
    """部分网关把入参作为 JSON 字符串给出。"""
    events = translate(clarification_call('{"question":"选哪个？","options":["A","B"]}'))

    assert isinstance(events[0], ClarificationRequest)
    assert events[0].options == ["A", "B"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        # strip + 去重，保持原顺序
        (["  React  ", "React", "Vue", "   ", "", "Vue"], ["React", "Vue"]),
        # 模型偶尔会传数字/None/dict：丢弃它们而不是整条拒绝
        (["A", 1, None, "B", {"x": 1}], ["A", "B"]),
    ],
)
def test_clarification_options_are_cleaned(raw, expected):
    events = translate(clarification_call({"question": "选哪个？", "options": raw}))

    assert events[0].options == expected


@pytest.mark.parametrize(
    "bad_args",
    [
        "not json at all",
        [],
        None,
    ],
)
def test_unparseable_clarification_warns_and_yields_nothing(caplog, bad_args):
    """入参根本不是 dict 时不能静默：既不发 ToolCall（前端会当成普通工具），也要留警告。"""
    with caplog.at_level(logging.WARNING, logger="app.services.chat_service"):
        events = translate(clarification_call(bad_args))

    assert events == []
    assert "意图确认工具入参无法解析" in caplog.text


@pytest.mark.parametrize(
    "bad_args,offending",
    [
        # question 侧
        ({}, "question"),
        ({"question": ""}, "question"),
        ({"question": "   "}, "question"),
        ({"question": 123}, "question"),
        ({"question": "问" * 501, "options": ["A", "B"]}, "question"),  # 超长
        # options 侧：至少 2 个**去重后非空**的选项，否则用户看到一个点不出结果的弹窗。
        # 期望词用 "option" 而不是 "options"：工具的文案有「候选项列表 (options)」与
        # 「单个候选项 (option)」两种，前者是后者的子串。
        ({"question": "选哪个？"}, "option"),
        ({"question": "选哪个？", "options": []}, "option"),
        ({"question": "选哪个？", "options": ["只有一个"]}, "option"),
        ({"question": "选哪个？", "options": ["A", "  ", ""]}, "option"),
        ({"question": "选哪个？", "options": ["A", "  A  "]}, "option"),
        ({"question": "选哪个？", "options": "A,B"}, "option"),  # 不是列表
        ({"question": "选哪个？", "options": [f"选项{i}" for i in range(11)]}, "option"),  # 超额
        ({"question": "选哪个？", "options": ["A" * 101, "B"]}, "option"),  # 单项超长
    ],
)
def test_invalid_clarification_args_are_refused_with_the_tool_reason(caplog, bad_args, offending):
    """能解析但过不了校验：不发弹窗，日志里记的是工具那份具体原因。

    与「无法解析」分开测是因为排障含义不同：前者说明模型的 JSON 坏了，后者说明
    参数越界——靠 reason 能直接看出是哪个参数。超限入参（问题过长、选项过多、
    单项过长）尤其必须在**翻译层**提前拒绝，不能先弹窗再让工具报错。
    """
    with caplog.at_level(logging.WARNING, logger="app.services.chat_service"):
        events = translate(clarification_call(bad_args))

    assert events == []
    assert "未通过校验" in caplog.text
    # reason 用的是工具原文，能定位到具体参数
    assert offending in caplog.text


def test_translate_and_tool_agree_on_the_same_rules():
    """同一个入参，工具与翻译层必须给出**一致**的通过/拒绝结论。"""
    from app.services.chat_service import _clarification_from
    from app.tools.ask_user_clarification import normalize_clarification

    cases = [
        ({"question": "选哪个？", "options": ["A", "B"]}, True),
        ({"question": "", "options": ["A", "B"]}, False),
        ({"question": "问" * 501, "options": ["A", "B"]}, False),
        ({"question": "选哪个？", "options": ["A"]}, False),
        ({"question": "选哪个？", "options": ["A", "B", "A"]}, True),  # 去重后 2 个
        ({"question": "选哪个？", "options": ["A" * 101, "B"]}, False),
        ({"question": "问", "options": ["  A  ", "", "B"]}, True),
    ]

    for args, should_pass in cases:
        tool_result = normalize_clarification(args["question"], args["options"])
        tool_passes = not isinstance(tool_result, str)
        event = _clarification_from(args, "call-1")
        translate_passes = event is not None

        assert tool_passes is should_pass, args
        assert translate_passes is should_pass, args


@pytest.mark.parametrize(
    "raw,expected",
    [
        # None = 客户端没传，默认允许自定义输入
        (None, True),
        (True, True),
        (False, False),
        # 回归 `bool("false") is True`：网关把布尔值当字符串回传时输入框会错误地打开
        ("true", True),
        ("1", True),
        ("false", False),
        ("FALSE", False),
        ("0", False),
        ("no", False),
        ("off", False),
        (" off ", False),
        ("", False),
    ],
)
def test_allow_custom_input_is_parsed_explicitly(raw, expected):
    args = {"question": "选哪个？", "options": ["A", "B"]}
    if raw is not None:
        args["allow_custom_input"] = raw

    events = translate(clarification_call(args))

    assert events[0].allow_custom_input is expected


# ---------------------------------------------------------------------
# 未知事件：R-2 的可观测性
#
# 这两类事件曾经落进兜底告警：`PartEndEvent` 与 `FinalResultEvent` 是**每次流式响应**
# 的固定成分（每个 part 结束时一条 / 每个正常回答至少一条），于是 `_warn_unknown_once`
# 在 100% 的正常流量上各打一行 WARNING。运维只会得出「这条告警没意义」的结论，而它
# 恰恰是发现「pydantic-ai 升级后渲染悄悄停摆」的唯一信号。
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "event",
    [
        PartEndEvent(index=0, part=TextPart(content="x")),
        FinalResultEvent(tool_name="t", tool_call_id="c"),
    ],
)
def test_routine_events_are_not_warned_about(caplog, event):
    """正常流量的固定成分不能被当成「未识别事件」。"""
    with caplog.at_level(logging.WARNING, logger="app.services.chat_service"):
        events = translate(event)

    assert events == []
    warnings = [record for record in caplog.records if "未处理的 pydantic-ai 流事件" in record.getMessage()]
    assert warnings == [], f"{type(event).__name__} 是常规事件，不该报警"


def test_genuinely_unknown_event_warns_once_per_type(caplog):
    """真正的未知类型必须继续告警——否则「渲染悄悄停摆」无从发现。

    生产日志级别是 INFO：原来的 DEBUG 等于没记，正文会静默丢光。按类型去重是为了
    让它可被发现，又不至于每个事件刷一行。
    """

    class UnknownEventA:
        pass

    class UnknownEventB:
        pass

    with caplog.at_level(logging.WARNING, logger="app.services.chat_service"):
        assert translate(UnknownEventA()) == []
        assert translate(UnknownEventA()) == []
        assert translate(UnknownEventB()) == []

    warnings = [record for record in caplog.records if "未处理的 pydantic-ai 流事件" in record.getMessage()]
    # 每种类型只告一次（去重），但不同类型都要告
    assert len(warnings) == 2
    assert all(record.levelno == logging.WARNING for record in warnings)
    # 文案要说明后果，否则看日志的人不知道该不该在意
    assert all("静默丢弃" in record.getMessage() for record in warnings)


# ---------------------------------------------------------------------
# 真实用量尾事件：不是「未识别事件」
# ---------------------------------------------------------------------
def terminal_event(*responses):
    """造一个尾部终态事件，带上只实现 `all_messages()` 的运行结果。"""

    class FakeRunResult:
        def __init__(self, messages):
            self._messages = list(messages)

        def all_messages(self):
            return self._messages

    return AgentRunResultEvent(result=FakeRunResult(responses))


def model_response(input_tokens=1_066, output_tokens=2, cache_read_tokens=896):
    from pydantic_ai.usage import RequestUsage

    return ModelResponse(
        parts=[TextPart(content="答")],
        usage=RequestUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
        ),
    )


def test_terminal_event_is_a_deliberate_ignore_not_an_unknown_event(caplog):
    """它带的是真实用量，不是「未识别的 pydantic-ai 流事件」。"""
    with caplog.at_level(logging.WARNING, logger="app.services.chat_service"):
        assert list(translate_event(terminal_event(model_response()))) == []

    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []


# 为什么用 `isinstance` 分派而不是 `type(event).__name__` 字符串比较：
# `FunctionToolCallEvent` 是 pydantic-ai 里 `ToolCallEvent` 的子类，按类名字符串
# 分派会漏掉以后新增的同类事件，升级改名时还会表现为「静默无输出」。这条只写在
# 注释里而不写成断言——它锁的是第三方库的继承关系，不是本项目的行为。


# ---------------------------------------------------------------------
# 序列化单点（R-1）
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "event",
    [
        ContentDelta(delta="你好"),
        ThinkingDelta(delta="嗯"),
        ToolCall(tool_call_id="c", tool_name="t", args='{"a":1}'),
        ToolResult(tool_call_id="c", tool_name="t", result="ok"),
        ClarificationRequest(tool_call_id="c", question="选？", options=["A"]),
        Usage(
            context_tokens=1_066,
            context_window=32_000,
            context_limit_tokens=18_000,
            output_tokens=20,
            cache_read_tokens=896,
        ),
        StreamError(message="出错了"),
        StreamError(message="上下文超了", retryable=False, code="context_overflow"),
        ContextCompacted(
            strategy="summarize",
            dropped_messages=4,
            truncated_messages=1,
            used_tokens_before=100_000,
            used_tokens_after=30_000,
            limit_tokens=40_000,
        ),
    ],
)
def test_sse_frame_is_data_only_and_newline_safe(event):
    """帧形状必须与前端 `\\r?\\n\\r?\\n` 分帧 + `data:` 收集兼容。"""
    raw = to_sse_frame(event).encode().decode()

    assert raw.startswith("data: ")
    assert raw.endswith("\r\n\r\n")
    payload = raw[len("data: ") : -len("\r\n\r\n")]
    assert "\n" not in payload and "\r" not in payload

    parsed = json.loads(payload)
    assert parsed["type"] == event.type


def test_usage_frame_carries_prompt_limit_for_progress_bar():
    """前端用 `context_limit_tokens` 当进度条分母；丢了它只能退回原始窗口。"""
    raw = (
        to_sse_frame(
            Usage(
                context_tokens=1_066,
                context_window=32_000,
                context_limit_tokens=18_000,
                output_tokens=20,
                cache_read_tokens=896,
            )
        )
        .encode()
        .decode()
    )

    parsed = json.loads(raw[len("data: ") : -len("\r\n\r\n")])
    assert parsed["context_limit_tokens"] == 18_000
    assert parsed["context_window"] == 32_000


def test_sse_frame_preserves_multiline_content():
    """正文里的换行被转义成 \\n，因此不会撕裂 SSE 分帧。"""
    raw = to_sse_frame(ContentDelta(delta="第一行\n第二行")).encode().decode()

    assert raw.count("\r\n\r\n") == 1
    assert json.loads(raw[len("data: ") : -len("\r\n\r\n")])["delta"] == "第一行\n第二行"


def test_sse_frame_keeps_non_ascii_readable():
    """ensure_ascii=False 的等价行为：中文不转义成 \\uXXXX（体积与可读性）。"""
    raw = to_sse_frame(ContentDelta(delta="中文")).encode().decode()

    assert "中文" in raw
    assert "\\u4e2d" not in raw


# ---------------------------------------------------------------------
# 失败分类（code）
# ---------------------------------------------------------------------
def test_stream_error_code_defaults_to_none():
    """`code` 必须可选：老客户端不认识它，不能因为多了一个字段就崩。"""
    assert StreamError(message="出错了").code is None


def test_stream_error_code_survives_serialization():
    """`type` 只区分「哪一类事件」，区分不了「哪一种失败」——判定要靠 code。"""
    raw = to_sse_frame(StreamError(message="超了", code="context_overflow")).encode().decode()
    parsed = json.loads(raw[len("data: ") : -len("\r\n\r\n")])

    assert parsed["code"] == "context_overflow"
    assert parsed["type"] == "error"


def test_unknown_stream_error_code_is_rejected():
    """写错分类名要在构造时就报错，而不是让客户端静默落到「不认识」分支。"""
    with pytest.raises(pydantic.ValidationError):
        StreamError(message="出错了", code="not_a_real_code")


# ---------------------------------------------------------------------
# 上下文压缩事件
# ---------------------------------------------------------------------
def test_compaction_event_survives_serialization():
    """压缩改了模型看到的上下文，前端必须能显示「发生了什么、省了多少」。"""
    raw = (
        to_sse_frame(
            ContextCompacted(
                strategy="summarize",
                dropped_messages=6,
                truncated_messages=2,
                used_tokens_before=120_000,
                used_tokens_after=30_000,
                limit_tokens=40_000,
            )
        )
        .encode()
        .decode()
    )

    parsed = json.loads(raw[len("data: ") : -len("\r\n\r\n")])
    assert parsed["type"] == "compaction"
    assert parsed["strategy"] == "summarize"
    assert parsed["dropped_messages"] == 6
    assert parsed["truncated_messages"] == 2
    assert parsed["used_tokens_before"] == 120_000
    assert parsed["used_tokens_after"] == 30_000


def test_compaction_event_rejects_unknown_strategy():
    """策略名写错要在构造时报错：前端按它决定文案，静默落到未知分支会显示空白。"""
    with pytest.raises(pydantic.ValidationError):
        ContextCompacted(
            strategy="rewrite",  # type: ignore[arg-type]
            used_tokens_before=1,
            used_tokens_after=1,
            limit_tokens=1,
        )


def test_compaction_event_has_no_digest_field():
    """摘要正文不进 SSE：它可能含早期对话的敏感内容，会被前端落库并同步到其他设备。"""
    payload = ContextCompacted(
        strategy="truncate",
        used_tokens_before=10,
        used_tokens_after=5,
        limit_tokens=100,
    ).model_dump()

    assert "digest" not in payload
    assert "summary" not in payload


def test_usage_frame_carries_cost_fields():
    """成本三个字段要真的出现在 SSE 帧里，而不是只存在于模型定义中。"""
    raw = (
        to_sse_frame(
            Usage(
                context_tokens=10_000,
                output_tokens=2_000,
                cache_read_tokens=4_000,
                cost_total=0.0492,
                cost_currency="USD",
                cost_cache_savings=0.0108,
            )
        )
        .encode()
        .decode()
    )

    parsed = json.loads(raw[len("data: ") : -len("\r\n\r\n")])
    assert parsed["cost_total"] == pytest.approx(0.0492)
    assert parsed["cost_currency"] == "USD"
    assert parsed["cost_cache_savings"] == pytest.approx(0.0108)


def test_usage_frame_omits_cost_when_unpriced():
    """没配价格时字段是 null——**不是 0**。

    0 会被前端显示成「$0.00」，读起来像免费；null 才会显示「未配置价格」。
    """
    raw = to_sse_frame(Usage(context_tokens=100, output_tokens=10)).encode().decode()

    parsed = json.loads(raw[len("data: ") : -len("\r\n\r\n")])
    assert parsed["cost_total"] is None
    assert parsed["cost_currency"] is None


# ---------------------------------------------------------------------
# 计时明细的线上契约
#
# `timing` 是「整块给或整块缺席」的一个块，前端按它显示「37 步 · 208 tok/s」。
# 这里的用例锁两件事：字段名/形状（前端镜像已冻结在 types.ts），以及
# 「缺省时是 null 而不是一堆 0」——0 会被读成明确的错误结论。
# ---------------------------------------------------------------------
def _payload(event):
    raw = to_sse_frame(event).encode().decode()
    return json.loads(raw[len("data: ") : -len("\r\n\r\n")])


def test_usage_frame_carries_full_timing_block():
    """齐全时七个字段全部下发，且字段名是**蛇形**（前端做 camelCase 映射）。"""
    parsed = _payload(
        Usage(
            context_tokens=10_000,
            output_tokens=2_000,
            timing=UsageTiming(
                steps=37,
                turn_ms=9_600,
                llm_ms=8_000,
                tool_ms=1_600,
                ttft_ms=420,
                decode_ms=7_600,
                decode_tokens=1_580,
            ),
        )
    )

    assert parsed["timing"] == {
        "steps": 37,
        "turn_ms": 9_600,
        "llm_ms": 8_000,
        "tool_ms": 1_600,
        "ttft_ms": 420,
        "decode_ms": 7_600,
        "decode_tokens": 1_580,
    }
    # 208 tok/s = 1580 / 7.6s，前端除得出来
    assert parsed["timing"]["decode_tokens"] / (parsed["timing"]["decode_ms"] / 1_000) == pytest.approx(207.9, abs=0.1)


def test_usage_frame_sends_null_timing_when_not_measured():
    """量不到时整块是 null。**不能**退化成一串 0：那会被读成「模型卡住了」。"""
    parsed = _payload(Usage(context_tokens=100, output_tokens=10))

    assert parsed["timing"] is None


def test_timing_block_allows_partial_decode_samples():
    """`ttft_ms`/`decode_ms`/`decode_tokens` 在**块内**允许为 null（其余照常给）。

    真实场景：网关一次性回整段文本时解码窗口不可测，但步数与总耗时仍然是真的。
    这时只隐藏速度那一项，不要让整块数字消失。
    """
    parsed = _payload(
        Usage(
            context_tokens=100,
            output_tokens=10,
            timing=UsageTiming(steps=3, turn_ms=900, llm_ms=800, tool_ms=100),
        )
    )

    assert parsed["timing"]["steps"] == 3
    assert parsed["timing"]["turn_ms"] == 900
    assert parsed["timing"]["ttft_ms"] is None
    assert parsed["timing"]["decode_ms"] is None
    assert parsed["timing"]["decode_tokens"] is None


def test_timing_keeps_a_real_zero_tool_ms():
    """`tool_ms` 的 0 是**测量结果**（一轮没调工具），必须原样下发。

    它与「量不到」不是一回事：量不到时整块 `timing` 缺席。把 0 也抹成 null
    会让「这轮没调工具」和「这轮没测到」在前端长得一模一样。
    """
    parsed = _payload(
        Usage(
            context_tokens=100,
            output_tokens=10,
            timing=UsageTiming(steps=1, turn_ms=500, llm_ms=500, tool_ms=0, ttft_ms=120),
        )
    )

    assert parsed["timing"]["tool_ms"] == 0


def test_timing_fields_are_integers():
    """协议层只用整毫秒：浮点秒会带一串尾数，前端还得自己 round。"""
    timing = UsageTiming(steps=2, turn_ms=1_234, llm_ms=1_000, tool_ms=234, ttft_ms=200)
    payload = timing.model_dump()

    for name in ("steps", "turn_ms", "llm_ms", "tool_ms", "ttft_ms"):
        assert isinstance(payload[name], int), name


def test_usage_timing_is_not_required_for_old_clients():
    """`timing` 有默认值：老服务端不发这个字段时，解析回来的对象仍然成立。"""
    parsed = Usage.model_validate({"type": "usage", "context_tokens": 42})

    assert parsed.timing is None
