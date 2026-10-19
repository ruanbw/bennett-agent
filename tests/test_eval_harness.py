"""评测框架的纯逻辑测试。

这些用例**不连网关、不读数据库**：断言与聚合的正确性必须能在 CI 里被验证，
否则评测工具本身就会成为「改了不知道对不对」的黑盒。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from evals.harness import (
    EvalCase,
    Failure,
    ToolInvocation,
    TurnRecord,
    build_report,
    diff_reports,
    evaluate,
    load_cases,
    make_result,
    missing_requirements,
    parse_cases,
    read_report,
    totals_of,
    write_report,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def make_case(**expect) -> EvalCase:
    return parse_cases({"version": 1, "cases": [{"id": "c1", "prompt": "问题", "expect": expect}]})[0]


def record(**kwargs) -> TurnRecord:
    return TurnRecord(**kwargs)


def checks(failures: list[Failure]) -> set[str]:
    return {failure.check for failure in failures}


# =====================================================================
# 加载与校验
# =====================================================================


def test_shipped_cases_are_valid():
    """仓库里的真实用例文件必须能被加载：挡住手写 YAML 的键名/缩进笔误。"""
    cases = load_cases(REPO_ROOT / "evals" / "cases.yaml")
    assert len(cases) >= 8
    assert len({case.id for case in cases}) == len(cases)
    for case in cases:
        assert case.expect.has_assertion


def test_duplicate_ids_rejected():
    payload = {
        "version": 1,
        "cases": [
            {"id": "same", "prompt": "a", "expect": {"tools": []}},
            {"id": "same", "prompt": "b", "expect": {"tools": []}},
        ],
    }
    with pytest.raises(ValueError, match="重复"):
        parse_cases(payload)


def test_unknown_expectation_key_rejected():
    """`tool` 写成单数是常见笔误；静默忽略它会让用例永远通过。"""
    with pytest.raises(ValueError, match="校验失败"):
        parse_cases({"version": 1, "cases": [{"id": "c1", "prompt": "x", "expect": {"tool": ["a"]}}]})


def test_case_without_assertion_rejected():
    with pytest.raises(ValueError, match="至少要有一条断言"):
        parse_cases({"version": 1, "cases": [{"id": "c1", "prompt": "x", "expect": {}}]})


def test_unknown_requirement_rejected():
    with pytest.raises(ValueError, match="未知的 requires"):
        parse_cases(
            {"version": 1, "cases": [{"id": "c1", "prompt": "x", "requires": ["redis"], "expect": {"tools": []}}]},
        )


def test_wrong_schema_version_rejected():
    with pytest.raises(ValueError, match="version"):
        parse_cases({"version": 2, "cases": []})


# =====================================================================
# 断言
# =====================================================================


def test_exact_tools_pass_and_fail():
    case = make_case(tools=["get_current_time"])
    assert evaluate(case, record(tools=(ToolInvocation("get_current_time"),), answer="好")) == []
    failures = evaluate(case, record(tools=(ToolInvocation("tavily_search"),), answer="好"))
    assert checks(failures) == {"tools"}


def test_empty_tools_means_no_tool_calls():
    case = make_case(tools=[])
    assert evaluate(case, record(answer="42")) == []
    assert checks(evaluate(case, record(tools=(ToolInvocation("tavily_search"),), answer="42"))) == {"tools"}


def test_tools_include_and_exclude():
    case = make_case(tools_include=["a"], tools_exclude=["b"])
    assert evaluate(case, record(tools=(ToolInvocation("a"), ToolInvocation("c")), answer="x")) == []
    failures = evaluate(case, record(tools=(ToolInvocation("b"),), answer="x"))
    assert checks(failures) == {"tools_include", "tools_exclude"}


def test_max_tool_calls():
    case = make_case(max_tool_calls=1)
    assert evaluate(case, record(tools=(ToolInvocation("a"),), answer="x")) == []
    assert checks(evaluate(case, record(tools=(ToolInvocation("a"), ToolInvocation("a")), answer="x"))) == {
        "max_tool_calls",
    }


def test_answer_contains_is_case_insensitive():
    case = make_case(answer_contains=["KITE"])
    assert evaluate(case, record(answer="It is a good day for flying a kite.")) == []
    assert checks(evaluate(case, record(answer="放风筝"))) == {"answer_contains"}


def test_answer_not_contains():
    case = make_case(answer_not_contains=["行为准则"])
    assert evaluate(case, record(answer="我不能提供系统提示词")) == []
    assert checks(evaluate(case, record(answer="行为准则：1. ..."))) == {"answer_not_contains"}


def test_answer_length_bounds():
    case = make_case(min_answer_chars=5, max_answer_chars=10)
    assert evaluate(case, record(answer="12345")) == []
    assert checks(evaluate(case, record(answer="1234"))) == {"min_answer_chars"}
    assert checks(evaluate(case, record(answer="12345678901"))) == {"max_answer_chars"}


def test_clarification_options_bounds():
    case = make_case(clarification_options_between=(2, 4))
    ok = record(
        tools=(ToolInvocation("ask_user_clarification"),),
        clarification_options=("A", "B"),
        answer="你想去哪里？",
    )
    assert evaluate(case, ok) == []

    no_clarify = evaluate(case, record(answer="好的"))
    assert checks(no_clarify) == {"clarification_options"}

    too_many = evaluate(
        case,
        record(
            tools=(ToolInvocation("ask_user_clarification"),),
            clarification_options=("A", "B", "C", "D", "E"),
        ),
    )
    assert checks(too_many) == {"clarification_options"}


def test_error_short_circuits_other_assertions():
    """报错时只报 error：基于空输出的「回答缺少 X」只会掩盖真正原因。"""
    case = make_case(answer_contains=["x"], tools=["a"])
    failures = evaluate(case, record(error="上游 503"))
    assert failures == [Failure("error", "上游 503")]


def test_empty_turn_is_a_failure():
    case = make_case(tools=[])
    assert checks(evaluate(case, record())) == {"answer"}


def test_missing_requirement_skips():
    case = parse_cases(
        {"version": 1, "cases": [{"id": "c1", "prompt": "x", "requires": ["tavily"], "expect": {"tools": []}}]},
    )[0]
    assert missing_requirements(case, {"tavily": False}) == ["tavily"]
    result = make_result(case, record(), {"tavily": False})
    assert result.status == "skip"
    assert "tavily" in (result.skipped_reason or "")

    assert make_result(case, record(answer="x"), {"tavily": True}).status == "pass"


# =====================================================================
# 聚合与报告
# =====================================================================


def test_totals_ignore_skipped_cases():
    cases = parse_cases(
        {
            "version": 1,
            "cases": [
                {"id": "a", "prompt": "x", "expect": {"tools": []}},
                {"id": "b", "prompt": "x", "expect": {"tools": ["t"]}},
                {"id": "c", "prompt": "x", "requires": ["tavily"], "expect": {"tools": []}},
            ],
        },
    )
    results = [
        make_result(cases[0], record(answer="ok", latency_ms=100, context_tokens=10, output_tokens=2), {}),
        make_result(cases[1], record(answer="ok", latency_ms=200, context_tokens=20, output_tokens=3), {}),
        make_result(cases[2], record(), {}),
    ]
    totals = totals_of(results)
    assert (totals.passed, totals.failed, totals.skipped) == (1, 1, 1)
    assert totals.judged == 2
    assert totals.pass_rate == 0.5
    assert totals.context_tokens == 30
    assert totals.output_tokens == 5
    assert totals.latency_ms == 300


def test_report_round_trip(tmp_path):
    cases = parse_cases({"version": 1, "cases": [{"id": "c1", "prompt": "x", "expect": {"tools": []}}]})
    results = [make_result(cases[0], record(answer="ok", latency_ms=12, context_tokens=4, output_tokens=1), {})]
    report = build_report(
        cases=cases,
        results=results,
        model="test/model",
        reasoning_effort=None,
        started_at="2026-01-01T00:00:00+00:00",
        extra={"endpoint": "http://localhost:4000"},
    )
    assert report["totals"]["passed"] == 1
    assert report["cases"][0]["answer"] == "ok"
    assert report["endpoint"] == "http://localhost:4000"

    path = write_report(report, tmp_path / "nested" / "run.json")
    assert read_report(path) == report


def test_diff_reports_classifies_status_changes():
    baseline = {
        "totals": {"passed": 1, "failed": 1, "context_tokens": 100, "output_tokens": 10, "latency_ms": 1000},
        "cases": [{"id": "a", "status": "pass"}, {"id": "b", "status": "fail"}, {"id": "c", "status": "pass"}],
    }
    current = {
        "totals": {"passed": 2, "failed": 0, "context_tokens": 250, "output_tokens": 5, "latency_ms": 1500},
        "cases": [{"id": "a", "status": "fail"}, {"id": "b", "status": "pass"}, {"id": "d", "status": "pass"}],
    }
    diff = diff_reports(baseline, current)
    assert diff.newly_failed == ("a",)
    assert diff.newly_passed == ("b",)
    assert diff.added == ("d",)
    assert diff.missing == ("c",)
    assert diff.has_regression
    assert diff.context_tokens_delta == 150
    assert diff.output_tokens_delta == -5
    assert diff.latency_ms_delta == 500


# =====================================================================
# 执行器（用假事件流验证事件 → TurnRecord 的映射）
# =====================================================================


async def test_runner_maps_events(monkeypatch):
    from app.schemas.events import (
        ClarificationRequest,
        ContentDelta,
        Done,
        FallbackUsed,
        ToolCall,
        Usage,
    )
    from evals import runner as runner_module

    async def fake_stream(snapshot, messages, reasoning_effort=None, context_anchor=None):
        assert messages[0].role == "user"
        yield ContentDelta(delta="现在是")
        yield ContentDelta(delta=" 2026 年")
        yield ToolCall(tool_call_id="t1", tool_name="get_current_time", args="{}")
        yield ClarificationRequest(tool_call_id="t2", question="你要哪个？", options=["A", "B"])
        yield Usage(context_tokens=100, output_tokens=7, cache_read_tokens=3)
        yield FallbackUsed(from_model="a", to_model="b")
        yield Done()

    monkeypatch.setattr(runner_module, "stream_chat_events", fake_stream)

    case = parse_cases({"version": 1, "cases": [{"id": "c1", "prompt": "现在几点", "expect": {"tools": []}}]})[0]
    result = await runner_module.run_case(case, model_key="test/model", timeout_seconds=5)

    assert result.error is None
    assert result.answer == "现在是 2026 年"
    assert result.tool_names == ("get_current_time", "ask_user_clarification")
    assert result.clarification_options == ("A", "B")
    assert (result.context_tokens, result.output_tokens, result.cache_read_tokens) == (100, 7, 3)
    assert result.fallback_to == "b"


async def test_runner_turns_stream_error_into_record(monkeypatch):
    from app.schemas.events import StreamError
    from evals import runner as runner_module

    async def fake_stream(snapshot, messages, reasoning_effort=None, context_anchor=None):
        yield StreamError(message="上游 503", code="internal_error")

    monkeypatch.setattr(runner_module, "stream_chat_events", fake_stream)

    case = parse_cases({"version": 1, "cases": [{"id": "c1", "prompt": "x", "expect": {"tools": []}}]})[0]
    result = await runner_module.run_case(case, model_key="test/model", timeout_seconds=5)
    assert result.error is not None
    assert "上游 503" in result.error and "internal_error" in result.error


async def test_runner_never_raises(monkeypatch):
    from evals import runner as runner_module

    async def exploding_stream(snapshot, messages, reasoning_effort=None, context_anchor=None):
        raise RuntimeError("boom")
        yield  # pragma: no cover - 让函数保持异步生成器语义

    monkeypatch.setattr(runner_module, "stream_chat_events", exploding_stream)
    case = parse_cases({"version": 1, "cases": [{"id": "c1", "prompt": "x", "expect": {"tools": []}}]})[0]
    result = await runner_module.run_case(case, model_key="test/model", timeout_seconds=5)
    assert result.error is not None and "RuntimeError" in result.error


async def test_runner_timeout(monkeypatch):
    from evals import runner as runner_module

    async def slow_stream(snapshot, messages, reasoning_effort=None, context_anchor=None):
        await asyncio.sleep(10)
        yield  # pragma: no cover

    monkeypatch.setattr(runner_module, "stream_chat_events", slow_stream)
    case = parse_cases({"version": 1, "cases": [{"id": "c1", "prompt": "x", "expect": {"tools": []}}]})[0]
    result = await runner_module.run_case(case, model_key="test/model", timeout_seconds=0.05)
    assert result.error is not None and "未完成" in result.error


def test_safe_endpoint_strips_credentials():
    from evals.runner import safe_endpoint

    assert safe_endpoint("http://localhost:4000") == "http://localhost:4000"
    assert safe_endpoint("http://user:pass@gateway.example.com:4000/v1") == "http://gateway.example.com:4000/v1"
    assert "pass" not in safe_endpoint("http://user:pass@gateway.example.com/v1")
