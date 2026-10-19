"""翻译层与序列化契约。

覆盖 R-1（类型化事件 + 单点序列化）与 R-2（`isinstance` 分派）。

翻译层是「pydantic-ai 说的事件」与「前端看得懂的事件」之间唯一的接缝。它此前用
`type(event).__name__` 字符串比较分派，升级改名后表现为「静默无输出」——没有任何
地方会报错。下面的用例既锁形状，也锁「不认识的事件会留下日志痕迹」。
"""

import json
import logging
import uuid

import pydantic
import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import (
    FinalResultEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
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

from app.api.chat import to_sse_frame
from app.schemas.events import (
    ClarificationRequest,
    ContentDelta,
    Done,
    StreamError,
    ThinkingDelta,
    ToolCall,
    ToolResult,
)
from app.services.chat_service import CLARIFICATION_TOOL_NAME, translate_event


def translate(event):
    return list(translate_event(event))


def describe(events):
    return [(event.type, vars(event)) for event in events]


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


def test_non_string_tool_result_is_serialised():
    event = FunctionToolResultEvent(
        part=ToolReturnPart(tool_name="t", content={"a": [1, 2]}, tool_call_id="c"),
    )

    assert json.loads(translate(event)[0].result) == {"a": [1, 2]}


def test_retry_prompt_result_is_translated():
    """工具报错走 RetryPromptPart，前端同样要能展示。"""
    event = FunctionToolResultEvent(
        part=RetryPromptPart(content="参数不合法", tool_name="t", tool_call_id="c"),
    )

    assert isinstance(translate(event)[0], ToolResult)


# ---------------------------------------------------------------------
# 意图确认提问（R-6 的接缝）
# ---------------------------------------------------------------------
def test_clarification_becomes_typed_event_not_tool_call():
    """提问必须是一等公民事件，前端不再比对 tool_name 字符串。"""
    events = translate(
        tool_call_event(
            CLARIFICATION_TOOL_NAME,
            {
                "question": "生成 React 还是 Vue？",
                "options": ["React", "Vue"],
                "allow_custom_input": True,
            },
            tool_call_id="ask-1",
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
    events = translate(tool_call_event(CLARIFICATION_TOOL_NAME, '{"question":"选哪个？","options":["A","B"]}'))

    assert isinstance(events[0], ClarificationRequest)
    assert events[0].options == ["A", "B"]


def test_clarification_defaults_allow_custom_input_to_true():
    events = translate(tool_call_event(CLARIFICATION_TOOL_NAME, {"question": "选哪个？", "options": ["A", "B"]}))

    assert events[0].allow_custom_input is True
    assert events[0].options == ["A", "B"]


def test_clarification_honours_explicit_false():
    events = translate(
        tool_call_event(
            CLARIFICATION_TOOL_NAME,
            {"question": "选哪个？", "options": ["A", "B"], "allow_custom_input": False},
        )
    )

    assert events[0].allow_custom_input is False


def test_clarification_filters_non_string_options():
    events = translate(tool_call_event(CLARIFICATION_TOOL_NAME, {"question": "选？", "options": ["A", 1, None, "B"]}))

    assert events[0].options == ["A", "B"]


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
        events = translate(tool_call_event(CLARIFICATION_TOOL_NAME, bad_args))

    assert events == []
    assert "意图确认工具入参无法解析" in caplog.text


@pytest.mark.parametrize(
    "bad_args",
    [
        {},
        {"question": ""},
        {"question": "   "},
        {"question": 123},
    ],
)
def test_invalid_clarification_args_are_refused_with_the_tool_reason(caplog, bad_args):
    """能解析但过不了校验：同样不发弹窗，但日志里记的是工具那份具体原因。

    两种情况分开测是因为它们的排障含义不同：「无法解析」说明模型的 JSON 坏了，
    「未通过校验」说明模型给的参数越界——后者靠 reason 能直接看出是哪个参数。
    """
    with caplog.at_level(logging.WARNING, logger="app.services.chat_service"):
        events = translate(tool_call_event(CLARIFICATION_TOOL_NAME, bad_args))

    assert events == []
    assert "未通过校验" in caplog.text
    # reason 用的是工具原文，能定位到具体参数
    assert "question" in caplog.text


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


def test_genuinely_unknown_event_still_warns(caplog):
    """真正的未知类型必须继续告警——否则「渲染悄悄停摆」无从发现。"""

    class TotallyUnknownEvent:
        pass

    with caplog.at_level(logging.WARNING, logger="app.services.chat_service"):
        events = translate(TotallyUnknownEvent())

    assert events == []
    assert "未处理的 pydantic-ai 流事件" in caplog.text
    assert "静默丢弃" in caplog.text


def test_str_name_dispatch_would_have_missed_subclasses():
    """说明为何不能用 type(event).__name__。

    FunctionToolCallEvent 是 ToolCallEvent 的子类；按类名字符串分派会漏掉
    以后新增的同类事件，而 isinstance 会自动接住。
    """
    from pydantic_ai.messages import ToolCallEvent

    assert issubclass(FunctionToolCallEvent, ToolCallEvent)
    assert type(FunctionToolCallEvent(part=ToolCallPart(tool_name="x", args={}))).__name__ != "ToolCallEvent"


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
        StreamError(message="出错了"),
        StreamError(message="上下文超了", retryable=False, code="context_overflow"),
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
# SSE 响应头：库强制写入的 Connection 必须被剥掉
# ---------------------------------------------------------------------
def fake_user():
    """未落库的临时用户，仅用于覆盖登录依赖（认证本身由 test_auth.py 覆盖）。"""
    from app.db import User

    return User(
        id=uuid.UUID("00000000-0000-4000-8000-000000000002"),
        email="sse-headers@example.com",
        hashed_password="not-a-real-hash",
        is_active=True,
        is_superuser=False,
        is_verified=False,
    )


def test_chat_sse_response_headers(monkeypatch):
    """真实响应上剥掉 `Connection`，并锁住不会被库默认值覆盖的缓存/缓冲标头。

    sse-starlette 3.5.0 在合并调用方 headers **之后**仍无条件写
    `Connection: keep-alive`，所以只断言 `SSE_HEADERS` 字典是伪保证；而 HTTP/2
    明确禁止该标头（RFC 7540 §8.1.2.2），网关可能因此重置流。
    """
    from app.api import chat as chat_api
    from app.config import settings
    from app.server import create_app
    from app.services.auth import current_active_user

    async def fake_stream(*_args, **_kwargs):
        yield Done()

    # 不打真实上游：本机 .env 配着网关，走到生成路径会真发一次请求
    monkeypatch.setattr(chat_api, "stream_chat_events", fake_stream)
    monkeypatch.setattr(settings, "chat_rate_limit_per_minute", 30)

    app = create_app()
    # 只替换登录态判断，路由、限流与响应构造都照真实路径执行
    app.dependency_overrides[current_active_user] = fake_user

    with TestClient(app) as client:
        response = client.post("/api/chat", json={"messages": [{"role": "user", "content": "你好"}]})

    assert response.status_code == 200
    assert "connection" not in response.headers
    assert response.headers["content-type"].startswith("text/event-stream")
    # SSE 不可复用：`no-cache` 会覆盖库默认的 `no-store`
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    # 半开连接兜底：库默认 send_timeout=None，等于不设超时
    assert chat_api.ChatEventSourceResponse(fake_stream()).send_timeout == chat_api.SSE_SEND_TIMEOUT_SECONDS


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
