"""自包含 HTML 报告：单文件、零外链、打开即看。

为什么模板用 f-string 而不是 jinja2，CSS/JS 全内联：

- 报告的读者是产线负责人、安全审计、算法同事，报告常常作为附件落在**没有外网**的机器上，
  或者贴进只能开内网工单系统的终端。任何 CDN（字体、图表库、favicon）都会让报告在那里退化成裸 HTML，
  而「附件在别人机器上排版全乱」这类问题不会在开发机上暴露。
- 模板文件多一个就是一个「发布时忘了打包」的事故源。一份 Python 模块自己产出整页 HTML，
  复制这一个文件就能跑。

为什么不用前端表格库：要排序的只有 4 列、要展开的只有一层。手写 60 行 JS 换来零依赖，
比引一个 40KB 的库更符合「企业内网报告」的约束。

安全口径：报告里混着模型输出、用户输入、工具返回、LLM 判分理由——全部不可信。
所有动态文本一律过 `html.escape`（含属性值），内联 JS 只读 `data-*` 属性、只改 `hidden`/class，
**不拼任何 HTML 字符串**：否则一条带 `<img onerror>` 的工具返回就能在审计员浏览器里执行。
"""

from __future__ import annotations

import html as _html
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from quality.models import DIMENSIONS, RunReport, SampleVerdict, Score, TaskSummary, Trace, TraceEvent

__all__ = ["render"]

#: 超过这个字符数的内容默认折叠。一条 8k 的工具返回展开后会把整条时间线推出屏幕，
#: 而报告的主要用途是「扫一眼看到哪一步坏了」。
_COLLAPSE_CHARS = 800

#: rubric 项的失败阈值。`RubricItem.threshold` 的默认值（见 models.py）：`SampleVerdict`
#: 只带分数不带阈值，报告又不允许反查用例，所以按默认阈值统计失败项次数。
_RUBRIC_FAIL_THRESHOLD = 0.6

_EVENT_KINDS = ("tool_call", "tool_result", "message", "guard", "error")

#: 指标卡顺序。前五项是「四维 + 综合分」，后两项看稳定性。
#: 刻意把 pass^k / pass@k 排在后面：它们不是维度分，混在一起会让人误以为通过率也是 0..1 的维度。
_METRIC_CARDS: tuple[tuple[str, str, str], ...] = (
    ("completion", "完成度", "rubric 加权分达线的任务比例"),
    ("robustness", "稳健性", "k 次全过才算稳（pass^k）"),
    ("safety", "安全性", "1 − 命中安全 gate 的样本比例"),
    ("efficiency", "性价比", "1 − 实际消耗 / 预算"),
    ("weighted_score", "综合分", "按任务权重加权平均"),
    ("pass_pow_k", "pass^k", "同一任务 k 次全部通过的比例"),
    ("pass_at_k", "pass@k", "k 次里至少一次通过的比例"),
)

#: gate 的 severity 定义在用例 YAML 的 `Gate` 上，不在 `RunReport` 里（models.py 冻结，
#: 报告不允许反查用例）。为了让安全失败在视觉上可分级，这里按 gate id 关键词给一个保守估计：
#: 认不出来的一律当 high——把不确定的当重的，比悄悄放过一条泄露更安全。
_GATE_SEVERITY_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("leak", "critical"),
    ("canary", "critical"),
    ("exfil", "critical"),
    ("ssrf", "critical"),
    ("secret", "critical"),
    ("destructive", "critical"),
    ("jailbreak", "critical"),
    ("inject", "critical"),
    ("budget", "medium"),
    ("timeout", "medium"),
    ("latency", "medium"),
)

_KIND_ORDER = {"gate": 0, "expectation": 1, "rubric": 2}
_KIND_LABEL = {"gate": "gate", "expectation": "断言", "rubric": "rubric"}

#: 单字段 payload 里可以直接取内容的键，省掉一层 JSON 噪声（工具调用读 args/result 最顺眼）。
_PAYLOAD_KEYS = ("result", "args", "content", "text", "message", "evidence", "detail")


# =====================================================================
# 入口
# =====================================================================


def render(report: RunReport, verdicts: list[SampleVerdict]) -> str:
    """把一次运行渲染成完整 HTML 文档。

    `verdicts` 允许为空（只从 report.json 出报告），也允许包含报告里不存在的任务
    （并行跑多套用例时上游拼接出错）——多出来的直接忽略，不能因为一串脏数据让整份报告出不来。
    """
    grouped = _group_verdicts(report, verdicts or [])
    body = [
        _header(report),
        "<main>",
        _metrics_section(report),
        _tasks_section(report, grouped),
        _traces_section(report, grouped),
        _failures_section(report, grouped),
        _cost_section(report),
        "</main>",
    ]
    return (
        "\n".join(
            [
                "<!DOCTYPE html>",
                '<html lang="zh-CN">',
                "<head>",
                '<meta charset="utf-8">',
                '<meta name="viewport" content="width=device-width, initial-scale=1">',
                f"<title>{esc(report.suite_id)} · {esc(report.run_id)} · 评测报告</title>",
                f"<style>{_CSS}</style>",
                "</head>",
                "<body>",
                *body,
                f"<script>{_JS}</script>",
                "</body>",
                "</html>",
            ]
        )
        + "\n"
    )


def _group_verdicts(report: RunReport, verdicts: Sequence[SampleVerdict]) -> dict[str, list[SampleVerdict]]:
    """按任务分组样本结论，丢弃报告里不存在的任务 id。

    丢弃而不是新建任务行：一个 report 里没有的任务没有成本、没有类别、没有权重，
    凭空插一行会让「通过率合计」和表里能看到的行对不上。
    """
    known = {task.task_id for task in report.tasks}
    grouped: dict[str, list[SampleVerdict]] = {}
    for verdict in verdicts:
        if verdict.task_id not in known:
            continue
        grouped.setdefault(verdict.task_id, []).append(verdict)
    for items in grouped.values():
        items.sort(key=lambda item: item.sample)
    return grouped


# =====================================================================
# 格式化
# =====================================================================


def esc(value: object) -> str:
    """不可信文本唯一出口。连引号一起转义，因为它也被塞进属性值里。"""
    return _html.escape(str(value), quote=True)


def _fmt_score(value: float | None) -> str:
    return "无" if value is None else f"{value:.2f}"


def _fmt_cost(value: float | None) -> str:
    return "无" if value is None else f"{value:.4f}"


def _fmt_duration(milliseconds: float | None) -> str:
    """耗时自适应：毫秒看抖动，秒看量级，分钟看超时。永远不要让人自己数零。"""
    if milliseconds is None:
        return "无"
    ms = max(0.0, float(milliseconds))
    if ms < 1000:
        return f"{ms:.0f}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    minutes, seconds = divmod(int(round(ms / 1000)), 60)
    return f"{minutes}m{seconds:02d}s"


def _fmt_number(value: float) -> str:
    if float(value).is_integer():
        return f"{value:,.0f}"
    return f"{value:,.4f}"


def _fmt_time(value: datetime) -> str:
    try:
        local = value.astimezone()
    except (ValueError, OSError):  # 过窄的时区库或伪时间戳：退回原样显示，不让报告挂掉
        return str(value)
    return local.strftime("%Y-%m-%d %H:%M:%S %z")


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _gate_severity(gate_id: str) -> str:
    lowered = gate_id.lower()
    for keyword, severity in _GATE_SEVERITY_KEYWORDS:
        if keyword in lowered:
            return severity
    return "high"


# =====================================================================
# 通用积木
# =====================================================================


def _section(section_id: str, title: str, hint: str, body: str) -> str:
    hint_html = f'<span class="hint">{esc(hint)}</span>' if hint else ""
    return f'<section id="{section_id}"><h2>{esc(title)}{hint_html}</h2>{body}</section>'


def _empty(text: str = "无") -> str:
    return f'<p class="empty">{esc(text)}</p>'


def _pre(text: str, css_class: str = "") -> str:
    """长文本折叠：默认收起，点开才占屏幕。折叠本身不发请求，`<details>` 原生可用。"""
    if not text:
        return _empty()
    class_attr = f' class="{css_class}"' if css_class else ""
    if len(text) > _COLLAPSE_CHARS:
        return (
            f'<details class="long"><summary>{len(text)} 字符，展开</summary>'
            f"<pre{class_attr}>{esc(text)}</pre></details>"
        )
    return f"<pre{class_attr}>{esc(text)}</pre>"


# =====================================================================
# 1. 头部
# =====================================================================


def _header(report: RunReport) -> str:
    total = sum(task.samples for task in report.tasks)
    passed = sum(task.passed_samples for task in report.tasks)
    if total:
        rate = passed / total
        chip = (
            f'<span class="chip {"pass" if rate >= 1.0 else "fail"}">'
            f"整体通过率 {rate * 100:.0f}%（{passed}/{total}）</span>"
        )
    else:
        chip = '<span class="chip is-empty">无样本</span>'

    facts: list[tuple[str, str, bool]] = [
        ("run_id", report.run_id, True),
        ("用例集", report.suite_id, False),
        ("目标", report.target, False),
        ("时间", _fmt_time(report.created_at), False),
        ("成本", f"${report.cost_usd:.4f}", True),
        ("时长", _fmt_duration(report.duration_ms), True),
        ("每任务样本", f"{report.samples} 次", False),
        ("suite 指纹", report.suite_fingerprint or "无", True),
        ("目标指纹", report.target_fingerprint or "无", True),
    ]
    for key in sorted(report.totals):
        facts.append((key, _fmt_number(report.totals[key]), True))

    cells = []
    for label, value, mono in facts:
        mono_class = ' class="mono"' if mono else ""
        cells.append(f"<div><dt>{esc(label)}</dt><dd{mono_class}>{esc(value)}</dd></div>")
    items = "".join(cells)
    return (
        '<header class="run">'
        f'<h1>评测报告 <span class="mono muted">{esc(report.target)}</span> {chip}</h1>'
        f'<dl class="facts">{items}</dl>'
        "</header>"
    )


# =====================================================================
# 2. 指标卡
# =====================================================================


def _metrics_section(report: RunReport) -> str:
    cards = []
    for key, label, hint in _METRIC_CARDS:
        value = report.metrics.get(key)
        cards.append(_metric_card(label, hint, None if value is None else float(value)))
    return _section(
        "metrics",
        "核心指标",
        "四维 + 综合分；pass^k / pass@k 看稳定性，不看单次运气",
        f'<div class="cards">{"".join(cards)}</div>',
    )


def _metric_card(label: str, hint: str, value: float | None) -> str:
    if value is None:
        return (
            '<div class="card is-empty">'
            f'<div class="card-label">{esc(label)}</div>'
            '<div class="card-value">无</div>'
            f'<div class="card-hint">{esc(hint)}</div></div>'
        )
    width = max(0.0, min(1.0, value)) * 100
    return (
        '<div class="card">'
        f'<div class="card-label">{esc(label)}</div>'
        f'<div class="card-value">{_fmt_score(value)}</div>'
        f'<div class="meter"><i style="width:{width:.0f}%"></i></div>'
        f'<div class="card-hint">{esc(hint)}</div></div>'
    )


# =====================================================================
# 3. 任务表
# =====================================================================


def _tasks_section(report: RunReport, grouped: dict[str, list[SampleVerdict]]) -> str:
    if not report.tasks:
        return _section("tasks", "任务明细", "", _empty())
    head = (
        "<thead><tr>"
        "<th>任务</th>"
        "<th>类别</th>"
        '<th class="num"><button type="button" class="sort" data-sort="pass" data-dir="none">通过率</button></th>'
        '<th class="num"><button type="button" class="sort" data-sort="score" data-dir="none">分数</button></th>'
        '<th class="num"><button type="button" class="sort" data-sort="cost" data-dir="none">成本</button></th>'
        '<th class="num"><button type="button" class="sort" data-sort="gates" data-dir="none">失败 gate</button></th>'
        '<th class="num">样本</th>'
        "</tr></thead>"
    )
    rows = "".join(_task_rows(task, grouped.get(task.task_id, [])) for task in report.tasks)
    table = f'<table class="tasks" id="task-table">{head}<tbody>{rows}</tbody></table>'
    return _section("tasks", "任务明细", "点表头排序，点行展开", table)


def _task_rows(task: TaskSummary, verdicts: list[SampleVerdict]) -> str:
    """每个任务固定输出「一行数据 + 一行明细」。交替两行才能让排序和展开都不破坏表格结构。"""
    gate_badges = "".join(_gate_badge(gate_id) for gate_id in task.gates_failed) or '<span class="empty">无</span>'
    main = (
        '<tr class="row" tabindex="0" aria-expanded="false"'
        f' data-task="{esc(task.task_id)}"'
        f' data-pass="{task.pass_rate:.6f}"'
        f' data-score="{task.score:.6f}"'
        f' data-cost="{task.cost_usd:.6f}"'
        f' data-gates="{len(task.gates_failed)}">'
        f'<td class="mono">{esc(task.task_id)}</td>'
        f"<td>{esc(task.category)}</td>"
        f'<td class="num">{task.passed_samples}/{task.samples}'
        f'<span class="muted"> ({task.pass_rate * 100:.0f}%)</span></td>'
        f'<td class="num">{_fmt_score(task.score)}</td>'
        f'<td class="num">{_fmt_cost(task.cost_usd)}</td>'
        f'<td class="num">{gate_badges}</td>'
        f'<td class="num">{task.samples}</td>'
        "</tr>"
    )
    detail = f'<tr class="detail" hidden><td colspan="7">{_task_detail(task, verdicts)}</td></tr>'
    return main + detail


def _task_detail(task: TaskSummary, verdicts: list[SampleVerdict]) -> str:
    dims = "".join(
        f"<li>{esc(dimension)} <b>{_fmt_score(task.dimension_scores[dimension])}</b></li>"
        for dimension in DIMENSIONS
        if dimension in task.dimension_scores
    )
    failed = "".join(f'<span class="badge fail">{esc(item_id)}</span>' for item_id in task.failed_items)
    failed += "".join(_gate_badge(gate_id) for gate_id in task.gates_failed)
    failed_html = failed or '<span class="empty">无</span>'

    if verdicts:
        samples = "".join(_sample_line(verdict) for verdict in verdicts)
        samples_html = f'<ul class="samples">{samples}</ul>'
    else:
        samples_html = _empty("报告未附样本明细")

    return (
        '<div class="detail-grid">'
        f"<div><h4>各样本</h4>{samples_html}</div>"
        f'<div><h4>维度分</h4><ul class="dims">{dims}</ul></div>'
        f'<div><h4>失败项</h4><div class="chips">{failed_html}</div></div>'
        "</div>"
    )


def _sample_line(verdict: SampleVerdict) -> str:
    badge = '<span class="badge pass">通过</span>' if verdict.passed else '<span class="badge fail">失败</span>'
    reasons = [f"gate:{gate}" for gate in verdict.gates_failed]
    reasons += [f"断言:{reason}" for reason in verdict.expectation_failures]
    reason_html = f'<span class="muted">{esc("；".join(reasons))}</span>' if reasons else ""
    trace = verdict.trace
    return (
        f'<li>{badge} <span class="mono">#{verdict.sample}</span>'
        f" · 分 {_fmt_score(verdict.score)}"
        f" · {_fmt_duration(trace.latency_ms)}"
        f" · ${_fmt_cost(trace.cost_usd)} {reason_html}</li>"
    )


def _gate_badge(gate_id: str) -> str:
    severity = _gate_severity(gate_id)
    return f'<span class="badge sev-{severity}">{esc(gate_id)}</span>'


# =====================================================================
# 4. 轨迹查看器
# =====================================================================


def _traces_section(report: RunReport, grouped: dict[str, list[SampleVerdict]]) -> str:
    known = [task for task in report.tasks if grouped.get(task.task_id)]
    if not known:
        return _section("traces", "轨迹", "", _empty())

    blocks = []
    for task in known:
        samples = "".join(_trace_sample(verdict) for verdict in grouped[task.task_id])
        blocks.append(
            '<div class="trace-task">'
            f'<h3><span class="mono">{esc(task.task_id)}</span>'
            f'<span class="hint">{len(grouped[task.task_id])} 个样本</span></h3>'
            f"{samples}</div>"
        )
    return _section(
        "traces",
        "轨迹",
        "按样本展开：message / tool_call / tool_result / guard / error 的时间线",
        "".join(blocks),
    )


def _trace_sample(verdict: SampleVerdict) -> str:
    trace = verdict.trace
    badge = '<span class="badge pass">通过</span>' if verdict.passed else '<span class="badge fail">失败</span>'
    meta = (
        f'<span class="mono">#{verdict.sample}</span>'
        f'<span class="muted">分 {_fmt_score(verdict.score)}</span>'
        f'<span class="muted">{_fmt_duration(trace.latency_ms)}</span>'
        f'<span class="muted">${_fmt_cost(trace.cost_usd)}</span>'
        f'<span class="muted">{trace.input_tokens:,}+{trace.output_tokens:,} tok</span>'
    )
    return (
        '<details class="sample">'
        f"<summary>{badge}{meta}</summary>"
        f'<div class="sample-body">{_trace_body(trace)}{_scores_table(verdict.scores)}</div>'
        "</details>"
    )


def _trace_body(trace: Trace) -> str:
    parts = []
    if trace.error:
        parts.append(f'<div class="callout fail"><h4>执行错误</h4>{_pre(trace.error, "error")}</div>')
    if trace.guard_events:
        guards = "".join(f"<li>{esc(item)}</li>" for item in trace.guard_events)
        parts.append(f'<div class="callout guard"><h4>护栏/降级事件</h4><ul>{guards}</ul></div>')
    if trace.canary:
        # canary 是探针注入的标记串，出现在回答或工具参数里就是泄露；报告里要能对照着查。
        parts.append(f'<p class="muted">环境标记（canary）：<span class="mono">{esc(trace.canary)}</span></p>')

    if trace.events:
        events = "".join(_event_html(event) for event in trace.events)
        parts.append(f'<ol class="timeline">{events}</ol>')
    else:
        parts.append(_empty("无事件"))

    parts.append(f"<h4>最终回答</h4>{_pre(trace.final_answer, 'answer')}")
    return "".join(parts)


def _event_html(event: TraceEvent) -> str:
    kind = event.kind if event.kind in _EVENT_KINDS else "message"
    head = [
        f'<span class="ev-at">{_fmt_duration(event.at_ms)}</span>',
        f'<span class="ev-kind">{esc(kind)}</span>',
    ]
    if event.name:
        head.append(f'<span class="ev-name">{esc(event.name)}</span>')
    body = _payload_text(event.payload)
    body_html = _pre(body, "payload") if body else '<span class="ev-empty">无</span>'
    return f'<li class="ev ev-{kind}"><div class="ev-head">{"".join(head)}</div>{body_html}</li>'


def _payload_text(payload: dict[str, Any]) -> str:
    if not payload:
        return ""
    if len(payload) == 1:
        key, value = next(iter(payload.items()))
        if key in _PAYLOAD_KEYS:
            return value if isinstance(value, str) else _json_text(value)
    return _json_text(payload)


def _scores_table(scores: list[Score]) -> str:
    if not scores:
        return ""
    rows = []
    for score in scores:
        rationale = esc(score.rationale) if score.rationale else '<span class="empty">无</span>'
        rows.append(
            "<tr>"
            f'<td class="mono">{esc(score.item_id)}</td>'
            f"<td>{esc(score.dimension)}</td>"
            f'<td class="num">{_fmt_score(score.value)}</td>'
            f"<td>{esc(score.source)}</td>"
            f"<td>{rationale}</td>"
            "</tr>"
        )
    return (
        '<h4>判分明细</h4><table class="scores"><thead><tr>'
        '<th>rubric 项</th><th>维度</th><th class="num">得分</th><th>来源</th><th>理由</th>'
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


# =====================================================================
# 5. 失败项汇总
# =====================================================================


@dataclass
class _FailureRow:
    kind: str
    key: str
    count: int = 0
    tasks: set[str] = field(default_factory=set)
    detail: str = ""
    severity: str = ""

    @property
    def sort_key(self) -> tuple[int, int, str]:
        return (-self.count, _KIND_ORDER.get(self.kind, 9), self.key)


def _failure_rows(report: RunReport, grouped: dict[str, list[SampleVerdict]]) -> list[_FailureRow]:
    """把三类失败（gate / 断言 / rubric）按「同一原因出现多少次」聚起来。

    调优要改的是共因。列成一长串个案，人会按最后看到的那条改提示词。
    """
    rows: dict[tuple[str, str], _FailureRow] = {}
    counted_tasks: dict[tuple[str, str], set[str]] = {}

    def bump(kind: str, key: str, task_id: str, *, count: int = 1, detail: str = "", severity: str = "") -> None:
        row = rows.get((kind, key))
        if row is None:
            row = rows[(kind, key)] = _FailureRow(kind=kind, key=key, severity=severity)
        row.count += count
        row.tasks.add(task_id)
        if detail and not row.detail:
            row.detail = detail
        counted_tasks.setdefault((kind, key), set()).add(task_id)

    for task in report.tasks:
        for verdict in grouped.get(task.task_id, []):
            if verdict.gates_failed:
                for gate_id in verdict.gates_failed:
                    bump("gate", gate_id, task.task_id, severity=_gate_severity(gate_id))
            if verdict.expectation_failures:
                for reason in verdict.expectation_failures:
                    bump("expectation", reason, task.task_id)
            for score in verdict.scores:
                if score.value < _RUBRIC_FAIL_THRESHOLD:
                    rationale = f"判分理由：{score.rationale}" if score.rationale else ""
                    bump("rubric", score.item_id, task.task_id, detail=rationale)

    # 报告自带的任务汇总是**权威清单**（runner 按真实阈值算的）。样本明细缺失、或者阈值不是
    # 默认值 0.6 时，只靠 verdicts 统计会漏项，所以这里用汇总补齐——宁可次数粗一点，也不要漏掉失败项。
    for task in report.tasks:
        failed_samples = max(1, task.samples - task.passed_samples)
        for item_id in task.failed_items:
            if task.task_id in counted_tasks.get(("rubric", item_id), set()):
                continue
            bump(
                "rubric",
                item_id,
                task.task_id,
                count=failed_samples,
                detail="由任务汇总给出（阈值非 0.6 或缺少样本明细）",
            )
        for gate_id in task.gates_failed:
            if task.task_id in counted_tasks.get(("gate", gate_id), set()):
                continue
            bump(
                "gate",
                gate_id,
                task.task_id,
                count=failed_samples,
                severity=_gate_severity(gate_id),
                detail="由任务汇总给出（缺样本明细）",
            )

    return sorted(rows.values(), key=lambda row: row.sort_key)


def _failures_section(report: RunReport, grouped: dict[str, list[SampleVerdict]]) -> str:
    rows = _failure_rows(report, grouped)
    if not rows:
        return _section("failures", "失败项汇总", "按出现次数排序", _empty())
    body_rows = []
    for row in rows:
        detail = esc(row.detail) if row.detail else '<span class="empty">无</span>'
        body_rows.append(
            "<tr>"
            f"<td>{esc(_KIND_LABEL.get(row.kind, row.kind))}</td>"
            f'<td class="mono">{esc(row.key)}</td>'
            f'<td class="num">{row.count}</td>'
            f"<td>{_severity_cell(row.severity)}</td>"
            f'<td class="mono">{esc("、".join(sorted(row.tasks)))}</td>'
            f"<td>{detail}</td>"
            "</tr>"
        )
    table = (
        '<table class="failures"><thead><tr>'
        '<th>类型</th><th>原因</th><th class="num">次数</th><th>严重度</th><th>涉及任务</th><th>说明/证据</th>'
        f"</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"
    )
    return _section(
        "failures",
        "失败项汇总",
        "按出现次数排序：先改共因；rubric 项得分低于 0.6 计一次失败",
        table,
    )


def _severity_cell(severity: str) -> str:
    if not severity:
        return '<span class="empty">无</span>'
    return f'<span class="badge sev-{esc(severity)}">{esc(severity)}</span>'


# =====================================================================
# 6. 成本分解
# =====================================================================


def _cost_section(report: RunReport) -> str:
    if not report.tasks:
        return _section("costs", "成本分解", "按任务", _empty())
    total = sum(task.cost_usd for task in report.tasks)
    rows_by_cost = sorted(report.tasks, key=lambda task: (-task.cost_usd, task.task_id))
    body_rows = "".join(
        "<tr>"
        f'<td class="mono">{esc(task.task_id)}</td>'
        f"<td>{esc(task.category)}</td>"
        f'<td class="num">{task.samples}</td>'
        f'<td class="num">{_fmt_cost(task.cost_usd)}</td>'
        f'<td class="num">{_share(task.cost_usd, total)}</td>'
        f'<td class="share"><span style="width:{_share_width(task.cost_usd, total):.0f}%"></span></td>'
        "</tr>"
        for task in rows_by_cost
    )
    table = (
        '<table class="costs"><thead><tr>'
        '<th>任务</th><th>类别</th><th class="num">样本</th><th class="num">成本（USD）</th>'
        f'<th class="num">占比</th><th></th></tr></thead><tbody>{body_rows}</tbody></table>'
        f'<p class="muted">合计 ${_fmt_cost(total)}；报告口径 ${_fmt_cost(report.cost_usd)}</p>'
    )
    return _section("costs", "成本分解", "按任务：先看谁在烧钱，再看烧得值不值", table)


def _share(value: float, total: float) -> str:
    if total <= 0:
        return "无"
    return f"{value / total * 100:.1f}%"


def _share_width(value: float, total: float) -> float:
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, value / total)) * 100


# =====================================================================
# 样式
# =====================================================================

_CSS = """
:root{
  color-scheme:light dark;
  --bg:#f6f7f9; --surface:#fff; --surface-2:#f0f2f5; --line:#dcdfe5;
  --text:#1b1f24; --muted:#697180; --accent:#2563eb;
  --pass:#15803d; --pass-bg:#e9f7ee; --fail:#b42318; --fail-bg:#fdecea;
  --sev-critical:#b42318; --sev-critical-bg:#fdecea;
  --sev-high:#b45309; --sev-high-bg:#fdf3e6;
  --sev-medium:#8a6a08; --sev-medium-bg:#fbf6e2;
  --sev-low:#4b5563; --sev-low-bg:#eef0f3;
  --mono:"SFMono-Regular",ui-monospace,"JetBrains Mono",Menlo,Consolas,"Noto Sans Mono CJK SC",monospace;
  --sans:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",
         "Source Han Sans SC","Noto Sans CJK SC",system-ui,sans-serif;
}
@media (prefers-color-scheme:dark){
  :root{
    --bg:#131619; --surface:#1a1f23; --surface-2:#22282d; --line:#2d353b;
    --text:#e5e8ea; --muted:#98a2ad; --accent:#7aa7ff;
    --pass:#5ddc8b; --pass-bg:#16301f; --fail:#ff8b80; --fail-bg:#3a1d1b;
    --sev-critical:#ff8b80; --sev-critical-bg:#3a1d1b;
    --sev-high:#ffc078; --sev-high-bg:#372a17;
    --sev-medium:#e2cc7a; --sev-medium-bg:#302b17;
    --sev-low:#b3bcc6; --sev-low-bg:#262d33;
  }
}
*{box-sizing:border-box}
body{
  margin:0;padding:24px clamp(12px,4vw,40px) 64px;background:var(--bg);color:var(--text);
  font:14px/1.55 var(--sans);-webkit-font-smoothing:antialiased;
}
h1{font-size:20px;margin:0 0 4px;display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
h2{font-size:15px;margin:0 0 10px;display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
h2 .hint,h3 .hint{font-size:12px;color:var(--muted);font-weight:400}
h3{font-size:13px;margin:0 0 8px;display:flex;gap:8px;align-items:baseline}
h4{font-size:12px;margin:12px 0 4px;color:var(--muted);font-weight:600;letter-spacing:.02em}
h4:first-child{margin-top:0}
section{margin-top:26px}
main{max-width:1440px}
.mono,pre,.ev-at,.ev-name{font-family:var(--mono);font-size:12px}
.muted{color:var(--muted)}
.empty{color:var(--muted);margin:0}
pre{
  margin:4px 0 0;padding:8px 10px;background:var(--surface-2);border-radius:6px;
  white-space:pre-wrap;word-break:break-word;max-height:460px;overflow:auto;
}
header.run{border-bottom:1px solid var(--line);padding-bottom:14px;max-width:1440px}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:4px 18px;margin:12px 0 0}
.facts div{display:flex;gap:8px;min-width:0}
.facts dt{color:var(--muted);flex:none}
.facts dd{margin:0;overflow-wrap:anywhere}
.chip{
  display:inline-block;padding:1px 9px;border-radius:999px;font-size:12px;
  border:1px solid var(--line);font-weight:500;
}
.chip.pass{color:var(--pass);background:var(--pass-bg);border-color:var(--pass)}
.chip.fail{color:var(--fail);background:var(--fail-bg);border-color:var(--fail)}
.chip.is-empty{color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(152px,1fr));gap:10px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.card-label{font-size:12px;color:var(--muted)}
.card-value{font-size:22px;font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums}
.card-hint{font-size:11px;color:var(--muted);margin-top:4px}
.card.is-empty .card-value{font-size:17px;color:var(--muted);font-weight:400}
.meter{height:4px;background:var(--surface-2);border-radius:999px;margin-top:6px;overflow:hidden}
.meter i{display:block;height:100%;background:var(--accent)}
table{
  width:100%;border-collapse:collapse;background:var(--surface);
  border:1px solid var(--line);border-radius:8px;overflow:hidden;
}
thead th{
  text-align:left;font-size:12px;color:var(--muted);font-weight:600;background:var(--surface-2);
  padding:6px 10px;border-bottom:1px solid var(--line);white-space:nowrap;
}
tbody td{padding:6px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tbody tr:last-child td{border-bottom:0}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
tr.row{cursor:pointer}
tr.row:hover td{background:var(--surface-2)}
tr.row:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
tr.row[aria-expanded="true"] td{background:var(--surface-2)}
tr.detail td{background:var(--surface-2);padding:12px}
button.sort{
  background:none;border:0;padding:0;font:inherit;color:inherit;cursor:pointer;
  display:inline-flex;gap:4px;align-items:center;
}
button.sort::after{content:"\\2195";opacity:.4;font-size:11px}
button.sort[data-dir="desc"]::after{content:"\\2193";opacity:1;color:var(--accent)}
button.sort[data-dir="asc"]::after{content:"\\2191";opacity:1;color:var(--accent)}
.badge{
  display:inline-block;padding:0 6px;border-radius:4px;font-size:11px;line-height:18px;
  border:1px solid transparent;white-space:nowrap;
}
.badge.pass{color:var(--pass);background:var(--pass-bg);border-color:var(--pass)}
.badge.fail{color:var(--fail);background:var(--fail-bg);border-color:var(--fail)}
.badge.sev-critical,.sev-critical{color:var(--sev-critical);background:var(--sev-critical-bg);border-color:var(--sev-critical)}
.badge.sev-high,.sev-high{color:var(--sev-high);background:var(--sev-high-bg);border-color:var(--sev-high)}
.badge.sev-medium,.sev-medium{color:var(--sev-medium);background:var(--sev-medium-bg);border-color:var(--sev-medium)}
.badge.sev-low,.sev-low{color:var(--sev-low);background:var(--sev-low-bg);border-color:var(--sev-low)}
.detail-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}
.samples,.dims,.callout ul{margin:0;padding-left:16px}
.samples li,.dims li{margin:2px 0;overflow-wrap:anywhere}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.trace-task{margin-bottom:14px}
details.sample{
  background:var(--surface);border:1px solid var(--line);border-radius:8px;
  padding:8px 12px;margin-bottom:8px;
}
details.sample>summary{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;list-style:none;cursor:pointer}
details.sample>summary::-webkit-details-marker{display:none}
details.sample>summary::before{content:"\\25B8";color:var(--muted);font-size:11px}
details.sample[open]>summary::before{content:"\\25BE"}
details.sample[open]>summary{margin-bottom:10px;padding-bottom:8px;border-bottom:1px dashed var(--line)}
details.long{margin-top:4px}
details.long>summary{color:var(--accent);font-size:12px;cursor:pointer}
.timeline{list-style:none;margin:8px 0 0;padding:0 0 0 4px;border-left:2px solid var(--line)}
.ev{position:relative;padding:4px 0 4px 14px}
.ev::before{
  content:"";position:absolute;left:-6px;top:11px;width:8px;height:8px;border-radius:50%;
  background:var(--muted);box-shadow:0 0 0 2px var(--surface);
}
.ev-tool_call::before{background:var(--accent)}
.ev-tool_result::before{background:var(--pass)}
.ev-guard::before{background:var(--sev-high)}
.ev-error::before{background:var(--fail)}
.ev-head{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.ev-at{color:var(--muted);min-width:58px}
.ev-kind{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.ev-tool_call .ev-name,.ev-tool_result .ev-name{color:var(--accent)}
.ev-empty{color:var(--muted);font-size:12px}
.callout{
  border-left:3px solid var(--line);padding:6px 10px;margin:10px 0;
  background:var(--surface-2);border-radius:0 6px 6px 0;
}
.callout.fail{border-left-color:var(--fail)}
.callout.guard{border-left-color:var(--sev-high)}
table.scores{margin-top:4px}
table.scores td{font-size:12px}
tbody td.share{width:120px;padding:10px 10px 0 0}
td.share span{display:block;height:6px;background:var(--accent);border-radius:999px;min-width:2px}
@media (max-width:640px){
  .facts{grid-template-columns:1fr}
  table{display:block;overflow-x:auto}
}
"""


# =====================================================================
# 脚本（内联，零依赖；只读 data-* 属性）
# =====================================================================

_JS = """
(function () {
  "use strict";
  var table = document.getElementById("task-table");
  if (!table || !table.tBodies.length) return;

  var body = table.tBodies[0];
  var rows = Array.prototype.slice.call(body.rows);
  var pairs = [];
  var i;
  for (i = 0; i + 1 < rows.length; i += 2) pairs.push([rows[i], rows[i + 1]]);
  if (!pairs.length) return;

  var sortKey = null;
  var sortDir = -1;

  function keyOf(row, key) {
    var value = parseFloat(row.getAttribute("data-" + key));
    return isNaN(value) ? -1 : value;
  }

  function paintHeaders() {
    var buttons = table.tHead ? table.tHead.querySelectorAll("button.sort") : [];
    for (var j = 0; j < buttons.length; j++) {
      var active = buttons[j].getAttribute("data-sort") === sortKey;
      buttons[j].setAttribute("data-dir", active ? (sortDir < 0 ? "desc" : "asc") : "none");
      buttons[j].setAttribute("aria-pressed", active ? "true" : "false");
    }
  }

  function reorder() {
    pairs.sort(function (a, b) {
      var delta = (keyOf(a[0], sortKey) - keyOf(b[0], sortKey)) * sortDir;
      if (delta !== 0) return delta;
      var left = a[0].getAttribute("data-task") || "";
      var right = b[0].getAttribute("data-task") || "";
      return left < right ? -1 : left > right ? 1 : 0;
    });
    var fragment = document.createDocumentFragment();
    for (var j = 0; j < pairs.length; j++) {
      fragment.appendChild(pairs[j][0]);
      fragment.appendChild(pairs[j][1]);
    }
    body.appendChild(fragment);
    paintHeaders();
  }

  function toggle(pair) {
    var main = pair[0];
    var detail = pair[1];
    var open = main.getAttribute("aria-expanded") === "true";
    main.setAttribute("aria-expanded", open ? "false" : "true");
    if (open) detail.setAttribute("hidden", "hidden");
    else detail.removeAttribute("hidden");
  }

  for (i = 0; i < pairs.length; i++) {
    (function (pair) {
      pair[0].addEventListener("click", function () { toggle(pair); });
      pair[0].addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " " || event.key === "Spacebar") {
          event.preventDefault();
          toggle(pair);
        }
      });
    })(pairs[i]);
  }

  var buttons = table.tHead ? table.tHead.querySelectorAll("button.sort") : [];
  for (i = 0; i < buttons.length; i++) {
    (function (button) {
      button.addEventListener("click", function (event) {
        event.stopPropagation();
        var key = button.getAttribute("data-sort");
        sortDir = key === sortKey ? -sortDir : -1;
        sortKey = key;
        reorder();
      });
    })(buttons[i]);
  }
})();
"""
