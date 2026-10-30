"""运行产物落盘：`report.json`（机器读）、`verdicts.jsonl`（逐行读）、`report.html`（人读）。

三种格式不是冗余，是三个消费方：

- `report.json`：后续的 `gate` 命令、调优闭环读它，结构必须稳定；
- `verdicts.jsonl`：一行一个样本，便于 `jq`/grep 在大文件里抽某一类失败，也便于
  边跑边追加（本项目目前整轮写完，但格式保持可追加）；
- `report.html`：单文件自包含，给人看，由 `quality.report.html` 实现（W4）。
  runner 只负责调用与落盘，不在这里拼 HTML——报告长得怎么样与跑得对不对是两件事，
  分开之后 HTML 可以独立改版而不动产物的产物结构。
"""

from __future__ import annotations

import json
from pathlib import Path

from quality.models import RunReport, SampleVerdict

REPORT_FILE = "report.json"
VERDICTS_FILE = "verdicts.jsonl"


def _dump_json(payload: object) -> str:
    """统一序列化口径：保留中文、缩进 2、结尾换行。

    `ensure_ascii=False` 是必须的：报告里全是中文 prompt，转义之后 diff 不可读，
    而报告进 git 之后的第一用途就是 diff。
    """
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _sorted_verdicts(report: RunReport, verdicts: list[SampleVerdict]) -> list[SampleVerdict]:
    """按「报告里的任务顺序 + 样本序号」排序。

    执行器是并发跑的，完成顺序取决于上游延迟，直接落盘会让两次同配置的运行产出
    不同顺序的文件，diff 一片红。报告里的任务顺序才是稳定的骨架。
    """
    order = {task.task_id: index for index, task in enumerate(report.tasks)}
    return sorted(verdicts, key=lambda verdict: (order.get(verdict.task_id, len(order)), verdict.sample))


def write_json(report: RunReport, verdicts: list[SampleVerdict], directory: Path) -> Path:
    """写出 `report.json` + `verdicts.jsonl`，返回**目录**（调用方据此拼其余产物路径）。"""
    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)

    (target_dir / REPORT_FILE).write_text(_dump_json(report.model_dump(mode="json")), encoding="utf-8")

    lines = [
        json.dumps(verdict.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        for verdict in _sorted_verdicts(report, verdicts)
    ]
    # 空列表也写空文件：下游 `for line in file` 不用区分「文件不存在」和「没有样本」。
    (target_dir / VERDICTS_FILE).write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return target_dir


def render_html(report: RunReport, verdicts: list[SampleVerdict], path: Path) -> Path:
    """调用 `quality.report.html.render` 生成单文件 HTML 并落盘，返回文件路径。

    这里延迟 import 是有意的：`quality/report/` 只依赖 `models.py`，而 runner 是它的
    上游。放在函数里，`quality.runner.report` 就能在没有 HTML 渲染器时照常 import
    （例如只在 CI 里跑 JSON 报告的流水线）。
    """
    from quality.report.html import render

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(report, _sorted_verdicts(report, verdicts)), encoding="utf-8")
    return target


__all__ = ["REPORT_FILE", "VERDICTS_FILE", "render_html", "write_json"]
