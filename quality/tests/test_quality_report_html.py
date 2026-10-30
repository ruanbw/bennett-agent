"""`quality.report.html.render` 的离线测试。

只测这一层的职责：把 RunReport + verdicts 变成一页能直接打开的 HTML，且不可信文本不会
变成可执行内容。不测样式细节（CSS 断言的寿命比样式短），只测结构契约、转义、容错与规模。
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone

from quality.models import RunReport, SampleVerdict, Score, TaskSummary, Trace, TraceEvent
from quality.report.html import render


def _summary(**overrides: object) -> TaskSummary:
    """一条任务的汇总。用例只覆盖自己关心的字段，其余取默认值。"""
    payload: dict[str, object] = {
        "task_id": "time-today",
        "category": "completion",
        "samples": 2,
        "passed_samples": 1,
        "score": 0.6666,
        "dimension_scores": {"completion": 0.6666},
        "cost_usd": 0.0123,
    }
    payload.update(overrides)
    return TaskSummary(**payload)


def _verdict(**overrides: object) -> SampleVerdict:
    """一次样本的结论 + 轨迹。轨迹覆盖 message/tool_call/tool_result 三种事件。"""
    trace = Trace(
        task_id="time-today",
        sample=0,
        events=[
            TraceEvent(kind="message", payload={"content": "帮我查一下今天几号"}, at_ms=0),
            TraceEvent(kind="tool_call", name="get_current_time", payload={"args": {"tz": "Asia/Shanghai"}}, at_ms=12),
            TraceEvent(kind="tool_result", name="get_current_time", payload={"result": "2026-03-05"}, at_ms=30),
        ],
        final_answer="今天是 2026-03-05。",
        latency_ms=640,
        input_tokens=1200,
        output_tokens=210,
        cost_usd=0.0021,
    )
    payload: dict[str, object] = {
        "task_id": "time-today",
        "sample": 0,
        "passed": True,
        "score": 0.9,
        "scores": [Score(item_id="uses_time_tool", dimension="completion", value=1.0, rationale="调用了时间工具")],
        "trace": trace,
    }
    payload.update(overrides)
    return SampleVerdict(**payload)


def _report(tasks: list[TaskSummary] | None = None, **overrides: object) -> RunReport:
    payload: dict[str, object] = {
        "run_id": "20260305-1200-abcdef",
        "created_at": datetime(2026, 3, 5, 4, 0, tzinfo=timezone.utc),
        "suite_id": "core-behavior",
        "suite_fingerprint": "3f2a1c0d9e8b7a65",
        "target": "prompt-lab",
        "target_fingerprint": "0011223344556677",
        "samples": 2,
        "tasks": [_summary()] if tasks is None else tasks,
        "metrics": {
            "completion": 0.5,
            "robustness": 0.0,
            "safety": 1.0,
            "efficiency": 0.81234,
            "pass_pow_k": 0.0,
            "pass_at_k": 0.5,
            "weighted_score": 0.6666,
        },
        "totals": {"input_tokens": 2400, "output_tokens": 420},
        "duration_ms": 1840,
        "cost_usd": 0.0123,
    }
    payload.update(overrides)
    return RunReport(**payload)


def _assert_document(document: str) -> None:
    assert document.startswith("<!DOCTYPE html>")
    assert document.rstrip().endswith("</html>")
    assert '<html lang="zh-CN">' in document


# =====================================================================
# 结构
# =====================================================================


def test_empty_report_renders_placeholders_instead_of_blank_sections() -> None:
    document = render(_report(tasks=[], metrics={}, totals={}, cost_usd=0.0, duration_ms=0), [])

    _assert_document(document)
    # 没有数据时每个区块都要有明确结论，不能留一片空白让人猜「是跑挂了还是没跑」
    assert "无" in document
    for section_id in ("metrics", "tasks", "traces", "failures", "costs"):
        assert f'id="{section_id}"' in document
    # 指标缺失时仍然要占住卡片位置，否则无法区分「为 0」与「没有这一项」
    assert document.count('class="card is-empty"') == 7


def test_header_shows_run_provenance_and_both_fingerprints() -> None:
    document = render(_report(), [_verdict()])

    for fragment in ("20260305-1200-abcdef", "core-behavior", "prompt-lab", "3f2a1c0d9e8b7a65", "0011223344556677"):
        assert fragment in document
    assert "2026-03-05" in document
    assert "1.8s" in document  # 1840ms 走秒级显示
    assert "整体通过率 50%（1/2）" in document


def test_metric_cards_cover_all_seven_metrics_and_use_two_decimals() -> None:
    document = render(_report(), [_verdict()])

    for label in ("完成度", "稳健性", "安全性", "性价比", "综合分", "pass^k", "pass@k"):
        assert label in document
    assert "0.50" in document
    assert "1.00" in document
    assert "0.81" in document  # 0.81234 截到两位，不四舍五入成两位以外的精度
    assert 'class="card is-empty"' not in document


# =====================================================================
# 转义（这是报告能否作为附件外发的底线）
# =====================================================================


def test_hostile_text_from_model_and_tools_is_escaped_everywhere() -> None:
    payload = "<script>alert(1)</script>"
    verdict = _verdict(
        scores=[
            Score(
                item_id="uses_time_tool",
                dimension="completion",
                value=0.2,
                rationale=f"中文理由 {payload}",
            )
        ],
        gates_failed=[f"canary_leak{payload}"],
        expectation_failures=[f"tools: 期望 {payload}"],
        trace=Trace(
            task_id="time-today",
            sample=1,
            events=[
                TraceEvent(kind="message", payload={"content": f"用户输入 {payload}"}, at_ms=0),
                TraceEvent(
                    kind="tool_result", name="tavily_search", payload={"result": f"外部数据 {payload}"}, at_ms=5
                ),
                TraceEvent(kind="error", payload={"detail": f"boom {payload}"}, at_ms=9),
            ],
            final_answer=f"模型回答 {payload}",
            latency_ms=10,
            cost_usd=0.001,
        ),
    )
    document = render(_report(), [verdict])

    # 一处不漏：payload 出现在回答、工具返回、错误、判分理由、失败原因里
    assert "<script>alert" not in document
    assert payload not in document
    assert document.count("&lt;script&gt;alert(1)&lt;/script&gt;") >= 5


def test_html_is_self_contained_with_no_remote_assets() -> None:
    document = render(_report(), [_verdict()])

    assert not re.search(r"https?://", document), "报告不能引用任何外链（企业内网常无外网）"
    assert "<link" not in document and "@import" not in document
    assert "<script src" not in document
    assert "<img" not in document
    # 跟随系统主题 + 中文优先字体栈
    assert "prefers-color-scheme:dark" in document or "prefers-color-scheme: dark" in document
    assert "PingFang SC" in document


def test_inline_script_never_builds_html_from_data() -> None:
    document = render(_report(), [_verdict()])
    script = document[document.index("<script>") : document.index("</script>")]

    # 排序/展开只操作节点与属性；一旦出现 innerHTML，工具返回就能变成 DOM
    for forbidden in ("innerHTML", "outerHTML", "document.write", "insertAdjacentHTML"):
        assert forbidden not in script


# =====================================================================
# 任务表与轨迹
# =====================================================================


def test_task_table_exposes_sort_keys_and_expandable_detail_rows() -> None:
    document = render(_report(), [_verdict()])

    for key in ("pass", "score", "cost", "gates"):
        assert f'data-sort="{key}"' in document
    row = re.search(r'<tr class="row"[^>]*data-task="time-today"[^>]*>', document)
    assert row is not None
    assert 'data-pass="0.500000"' in row.group(0)
    assert 'data-score="0.666600"' in row.group(0)
    assert 'data-cost="0.012300"' in row.group(0)
    # 每个数据行后面必须紧跟一行明细，JS 按两条一组配对
    assert document.count('<tr class="row"') == document.count('<tr class="detail"')


def test_trace_viewer_renders_timeline_in_event_order() -> None:
    document = render(_report(), [_verdict()])

    kinds = re.findall(r'class="ev ev-([a-z_]+)"', document)
    assert kinds == ["message", "tool_call", "tool_result"]
    assert 'class="ev-at">12ms<' in document
    assert "今天是 2026-03-05。" in document
    assert "Asia/Shanghai" in document


def test_long_trace_is_collapsed_and_rendering_stays_fast() -> None:
    big_event = TraceEvent(kind="tool_result", name="tavily_search", payload={"result": "长内容" * 2000}, at_ms=1)
    trace = Trace(
        task_id="time-today",
        sample=0,
        events=[big_event for _ in range(400)],
        final_answer="x" * 20_000,
        latency_ms=90_000,
        cost_usd=0.5,
    )
    started = time.monotonic()
    document = render(_report(), [_verdict(trace=trace)])
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"超长轨迹渲染耗时 {elapsed:.1f}s，报告会拖垮 CLI"
    assert document.count("字符，展开") >= 401  # 每条长内容都折叠
    assert "1m30s" in document  # 90000ms 走分钟级显示


def test_verdicts_for_unknown_tasks_are_ignored_without_raising() -> None:
    orphan = _verdict(task_id="ghost-task", trace=Trace(task_id="ghost-task", sample=0, final_answer="幽灵回答"))
    document = render(_report(), [orphan])

    _assert_document(document)
    assert "ghost-task" not in document
    assert "幽灵回答" not in document
    # 报告里那条任务没有样本明细，但仍要出一行（否则表格会和 report.tasks 对不上）
    assert 'data-task="time-today"' in document


def test_empty_verdicts_still_render_report_only() -> None:
    document = render(_report(), [])

    _assert_document(document)
    assert "报告未附样本明细" in document
    assert 'id="traces"' in document


# =====================================================================
# 失败汇总与成本
# =====================================================================


def test_failure_summary_counts_reasons_and_sorts_by_frequency() -> None:
    verdicts = [
        _verdict(
            sample=0,
            passed=False,
            gates_failed=["canary_no_leak"],
            expectation_failures=["tools: 缺少 get_current_time"],
        ),
        _verdict(
            sample=1,
            passed=False,
            gates_failed=["canary_no_leak"],
            expectation_failures=["tools: 缺少 get_current_time"],
        ),
        _verdict(sample=2, passed=False, gates_failed=[], expectation_failures=["tools: 缺少 get_current_time"]),
    ]
    report = _report(tasks=[_summary(samples=3, passed_samples=0)])
    document = render(report, verdicts)

    failures = document[document.index('id="failures"') : document.index('id="costs"')]
    # 断言条目按次数降序：tools 缺工具 3 次 > canary 2 次
    assert failures.index("tools: 缺少 get_current_time") < failures.index("canary_no_leak")
    assert '<td class="num">3</td>' in failures
    # 安全类 gate 必须带严重度标记，否则一条泄露和一条超时长得一样
    assert "badge sev-critical" in failures
    assert "critical" in failures


def test_security_gate_failures_are_marked_with_severity() -> None:
    verdict = _verdict(passed=False, gates_failed=["ssrf_attempt"])
    summary = _summary(passed_samples=0, gates_failed=["ssrf_attempt"])
    document = render(_report(tasks=[summary]), [verdict])

    assert 'class="badge sev-critical"' in document
    # 非安全类 gate 认不出关键词时按 high 处理，不能显示成和 critical 一样的红
    assert "sev-medium" in document  # CSS 里有定义
    document_high = render(_report(tasks=[_summary(gates_failed=["budget_exceeded"])]), [])
    assert 'sev-medium">budget_exceeded' in document_high


def test_cost_section_breaks_down_by_task_and_handles_zero_total() -> None:
    tasks = [
        _summary(task_id="time-today", cost_usd=0.03),
        _summary(task_id="no-tool-needed", cost_usd=0.01),
    ]
    document = render(_report(tasks=tasks), [])
    costs = document[document.index('id="costs"') :]

    assert "75.0%" in costs  # 0.03 / 0.04
    assert costs.index("time-today") < costs.index("no-tool-needed")  # 贵的排前面
    assert "0.0400" in costs

    zero = render(_report(tasks=[_summary(cost_usd=0.0)], cost_usd=0.0), [])
    assert "无" in zero[zero.index('id="costs"') :]
