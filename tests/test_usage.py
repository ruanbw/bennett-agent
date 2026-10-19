"""真实用量的提取口径。

这块的价值全在「取对那一个数字、拿不到就不报」两条上：一轮里如果有工具调用会有多次
模型请求，而每次都要重发一遍历史，所以只有**最后一次**请求的 `input_tokens` 等于
当前上下文长度；累加值（`RunUsage.input_tokens`）会把上下文算大。
"""

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.usage import RequestUsage

from app.services.usage import extract_turn_usage


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
