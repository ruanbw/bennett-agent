"""评测框架的纯逻辑：用例加载、断言、聚合、基线与报告。

**刻意不导入 `app` 的任何模块**：断言与聚合必须能在没有网关、没有数据库、没有
网络的环境下被测试（`tests/test_eval_harness.py` 就是靠这一点）。真正跑模型的
执行器在 `evals/runner.py`，它反过来依赖本模块。

判分口径是**行为**而不是文本相似度：

- 工具调用序列（`tools`）回答「该不该用工具、用的是哪一个」；
- 关键短语（`answer_contains`）只用于「答案里必须出现某个客观事实」这类粗检查；
- 文本质量本身不做自动判分——那是另一个量级的问题（需要 LLM judge），而它会引入
  自己的方差，先不做。

一条用例可以同时有多条断言，全部通过才算 PASS；任何一条不满足都会把原因写进
报告，避免「红了但不知道为什么」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

#: 用例文件格式版本。字段语义变化时要递增，避免旧文件被静默按新规则判分。
SCHEMA_VERSION = 1

#: 用例可以声明依赖的外部能力；执行器据此决定「跳过」而不是「失败」。
#: 目前只有 tavily：没配 key 时 `tavily_search` 根本不会被装配（见 app/tools/__init__.py），
#: 拿这种用例去判模型「没用工具」是冤枉它。
KNOWN_REQUIREMENTS = frozenset({"tavily"})

#: 会改变模型行为的运行配置。换思考等级、开关上下文守卫之后，同一模型也会给出不同
#: 的工具调用序列；拿不同配置的报告做基线，会把「配置不同」误报成「新失败」。
#: 自动基线按这三项筛选（见 `select_baseline`），显式基线不一致时先警告。
BEHAVIOR_FIELDS = ("model", "reasoning_effort", "context_guard")

Status = Literal["pass", "fail", "skip"]


class Expectation(BaseModel):
    """一条用例的期望。所有字段可选，但至少要有一种断言。"""

    model_config = ConfigDict(extra="forbid")

    #: 工具调用的**精确序列**。`[]` 表示「这一轮不该调用任何工具」，
    #: 与「不检查工具」的 `None` 是两件事，所以不能合并。
    tools: list[str] | None = None
    #: 这些工具都必须出现（不限制顺序与额外调用）。
    tools_include: list[str] = Field(default_factory=list)
    #: 这些工具都不许出现。
    tools_exclude: list[str] = Field(default_factory=list)
    #: 工具调用总次数上限（挡住「反复重试同一个工具」）。
    max_tool_calls: int | None = Field(default=None, ge=0)

    #: 回答必须/禁止包含的短语，大小写不敏感。
    answer_contains: list[str] = Field(default_factory=list)
    answer_not_contains: list[str] = Field(default_factory=list)
    min_answer_chars: int | None = Field(default=None, ge=0)
    max_answer_chars: int | None = Field(default=None, ge=0)

    #: 澄清提问的选项数量区间（含端点）。只在真的发起了澄清时生效。
    clarification_options_between: tuple[int, int] | None = None

    @model_validator(mode="after")
    def _validate_consistency(self) -> Expectation:
        overlap = set(self.tools_include) & set(self.tools_exclude)
        if overlap:
            raise ValueError(f"tools_include 与 tools_exclude 不能同时包含 {sorted(overlap)}")
        if not self.has_assertion:
            raise ValueError("expect 至少要有一条断言，否则这条用例永远不会红")
        if (
            self.min_answer_chars is not None
            and self.max_answer_chars is not None
            and self.min_answer_chars > self.max_answer_chars
        ):
            raise ValueError("min_answer_chars 不能大于 max_answer_chars")
        if self.clarification_options_between is not None:
            low, high = self.clarification_options_between
            if low < 1 or high < low:
                raise ValueError(f"clarification_options_between 必须满足 1 <= 下界 <= 上界，收到 {[low, high]}")
        return self

    @property
    def has_assertion(self) -> bool:
        """是否至少有了一条会被执行的断言。"""
        return bool(
            self.tools is not None
            or self.tools_include
            or self.tools_exclude
            or self.max_tool_calls is not None
            or self.answer_contains
            or self.answer_not_contains
            or self.min_answer_chars is not None
            or self.max_answer_chars is not None
            or self.clarification_options_between is not None
        )


class EvalCase(BaseModel):
    """一条评测用例。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9-]*$", description="用例 id，小写字母/数字/连字符")
    prompt: str = Field(min_length=1, description="发给模型的单轮用户消息")
    tags: list[str] = Field(default_factory=list, description="用于 --tag 过滤")
    requires: list[str] = Field(default_factory=list, description="外部能力依赖，缺失时 SKIP")
    expect: Expectation

    @field_validator("prompt", mode="before")
    @classmethod
    def _strip_prompt(cls, value: object) -> object:
        # 前导/尾随空白会原样进模型，容易让同一句话在不同用例里表现不同。
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _validate_requirements(self) -> EvalCase:
        unknown = [name for name in self.requires if name not in KNOWN_REQUIREMENTS]
        if unknown:
            raise ValueError(f"未知的 requires 取值 {unknown}，可选：{sorted(KNOWN_REQUIREMENTS)}")
        return self


def parse_cases(payload: Any) -> list[EvalCase]:
    """把 YAML 解析出来的结构转成用例列表，并做跨用例校验（id 唯一）。"""
    if not isinstance(payload, dict):
        raise ValueError("用例文件顶层必须是映射，且含 version 与 cases 两个键")

    version = payload.get("version")
    if version != SCHEMA_VERSION:
        raise ValueError(f"用例文件 version 必须是 {SCHEMA_VERSION}，收到 {version!r}")

    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("cases 必须是非空列表")

    unknown_keys = set(payload) - {"version", "cases"}
    if unknown_keys:
        raise ValueError(f"用例文件顶层出现未知键 {sorted(unknown_keys)}")

    cases: list[EvalCase] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_cases, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index} 条用例必须是映射")
        try:
            case = EvalCase.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(f"第 {index} 条用例（id={raw.get('id')!r}）校验失败：{exc}") from exc
        if case.id in seen:
            raise ValueError(f"用例 id 重复：{case.id}")
        seen.add(case.id)
        cases.append(case)
    return cases


def load_cases(path: str | Path) -> list[EvalCase]:
    """从 YAML 文件加载用例。"""
    file_path = Path(path)
    try:
        payload = yaml.safe_load(file_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"用例文件不存在：{file_path}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"用例文件不是合法 YAML（{file_path}）：{exc}") from exc
    return parse_cases(payload)


# =====================================================================
# 执行结果
# =====================================================================


@dataclass(frozen=True)
class ToolInvocation:
    """一次工具调用。`args` 保留原始字符串，便于在报告里复盘模型到底传了什么。"""

    name: str
    args: str = ""


@dataclass(frozen=True)
class TurnRecord:
    """一轮对话实际发生了什么。执行器填它，断言只读它。"""

    tools: tuple[ToolInvocation, ...] = ()
    answer: str = ""
    latency_ms: int = 0
    context_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    fallback_to: str | None = None
    error: str | None = None
    clarification_options: tuple[str, ...] = ()

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(invocation.name for invocation in self.tools)

    @property
    def total_tokens(self) -> int:
        return (self.context_tokens or 0) + (self.output_tokens or 0)


@dataclass(frozen=True)
class Failure:
    """一条未满足的断言。`check` 是机器可读的断言名，`detail` 给人看。"""

    check: str
    detail: str


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    status: Status
    failures: tuple[Failure, ...] = ()
    record: TurnRecord = field(default_factory=TurnRecord)
    skipped_reason: str | None = None


def missing_requirements(case: EvalCase, available: dict[str, bool]) -> list[str]:
    """用例声明了但当前环境没有的能力。"""
    return [name for name in case.requires if not available.get(name)]


def evaluate(case: EvalCase, record: TurnRecord) -> list[Failure]:
    """按用例期望检查一轮结果。返回空列表表示通过。

    一轮报错时**只**报这一条：后面的断言都会基于空输出给出误导性的失败
    （「回答缺少 X」），把真正的原因埋在噪音里。
    """
    if record.error:
        return [Failure("error", record.error)]

    expect = case.expect
    failures: list[Failure] = []
    names = list(record.tool_names)

    if expect.tools is not None and names != expect.tools:
        failures.append(Failure("tools", f"期望 {expect.tools}，实际 {names}"))

    if expect.tools_include:
        absent = [name for name in expect.tools_include if name not in names]
        if absent:
            failures.append(Failure("tools_include", f"缺少 {absent}，实际 {names}"))

    if expect.tools_exclude:
        present = [name for name in expect.tools_exclude if name in names]
        if present:
            failures.append(Failure("tools_exclude", f"不应调用 {present}"))

    if expect.max_tool_calls is not None and len(names) > expect.max_tool_calls:
        failures.append(
            Failure("max_tool_calls", f"调用 {len(names)} 次，上限 {expect.max_tool_calls}：{names}"),
        )

    if expect.clarification_options_between is not None:
        low, high = expect.clarification_options_between
        if not record.clarification_options:
            failures.append(Failure("clarification_options", "本轮没有发起带选项的澄清提问"))
        elif not low <= len(record.clarification_options) <= high:
            failures.append(
                Failure(
                    "clarification_options",
                    f"选项数 {len(record.clarification_options)}，期望 {low}~{high}",
                ),
            )

    answer = record.answer
    folded = answer.casefold()

    absent = [needle for needle in expect.answer_contains if needle.casefold() not in folded]
    if absent:
        failures.append(Failure("answer_contains", f"回答缺少 {absent}"))

    present = [needle for needle in expect.answer_not_contains if needle.casefold() in folded]
    if present:
        failures.append(Failure("answer_not_contains", f"回答出现了禁止内容 {present}"))

    if expect.min_answer_chars is not None and len(answer.strip()) < expect.min_answer_chars:
        failures.append(
            Failure("min_answer_chars", f"回答长度 {len(answer.strip())}，下限 {expect.min_answer_chars}"),
        )

    if expect.max_answer_chars is not None and len(answer.strip()) > expect.max_answer_chars:
        failures.append(
            Failure("max_answer_chars", f"回答长度 {len(answer.strip())}，上限 {expect.max_answer_chars}"),
        )

    # 隐式断言：既没有正文也没有工具调用的「空轮」不是合格回答。澄清轮允许正文很短，
    # 但它至少调用了工具，因此不会被这条误伤。
    if not answer.strip() and not names:
        failures.append(Failure("answer", "本轮既没有正文也没有工具调用"))

    return failures


def make_result(case: EvalCase, record: TurnRecord, available: dict[str, bool] | None = None) -> CaseResult:
    """把「用例 + 一轮结果」判成 PASS / FAIL / SKIP。"""
    missing = missing_requirements(case, available or {})
    if missing:
        return CaseResult(
            case_id=case.id,
            status="skip",
            skipped_reason=f"缺少依赖：{', '.join(missing)}",
        )
    failures = tuple(evaluate(case, record))
    return CaseResult(
        case_id=case.id,
        status="fail" if failures else "pass",
        failures=failures,
        record=record,
    )


@dataclass(frozen=True)
class Totals:
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    context_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    latency_ms: int = 0

    @property
    def judged(self) -> int:
        return self.passed + self.failed

    @property
    def pass_rate(self) -> float:
        return self.passed / self.judged if self.judged else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "pass_rate": round(self.pass_rate, 4),
            "context_tokens": self.context_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "latency_ms": self.latency_ms,
        }


def totals_of(results: list[CaseResult]) -> Totals:
    """汇总所有结果。跳过的用例不贡献时长与 token（它根本没跑）。"""
    passed = sum(1 for result in results if result.status == "pass")
    failed = sum(1 for result in results if result.status == "fail")
    skipped = sum(1 for result in results if result.status == "skip")
    judged = [result.record for result in results if result.status != "skip"]
    return Totals(
        passed=passed,
        failed=failed,
        skipped=skipped,
        context_tokens=sum(record.context_tokens or 0 for record in judged),
        output_tokens=sum(record.output_tokens or 0 for record in judged),
        cache_read_tokens=sum(record.cache_read_tokens or 0 for record in judged),
        latency_ms=sum(record.latency_ms for record in judged),
    )


# =====================================================================
# 报告与基线
# =====================================================================


def build_report(
    *,
    cases: list[EvalCase],
    results: list[CaseResult],
    model: str,
    reasoning_effort: str | None,
    started_at: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """组装可落盘的报告。字段名是给脚本读的，不做翻译。"""
    if len(cases) != len(results):
        raise ValueError(f"用例与结果数量不一致：{len(cases)} vs {len(results)}")

    case_entries = []
    for case, result in zip(cases, results, strict=True):
        record = result.record
        case_entries.append(
            {
                "id": case.id,
                "tags": list(case.tags),
                "status": result.status,
                "skipped_reason": result.skipped_reason,
                "failures": [{"check": failure.check, "detail": failure.detail} for failure in result.failures],
                "tools": [{"name": invocation.name, "args": invocation.args} for invocation in record.tools],
                "clarification_options": list(record.clarification_options),
                "latency_ms": record.latency_ms,
                "context_tokens": record.context_tokens,
                "output_tokens": record.output_tokens,
                "cache_read_tokens": record.cache_read_tokens,
                "fallback_to": record.fallback_to,
                "error": record.error,
                "answer": record.answer,
            },
        )

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "started_at": started_at,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "totals": totals_of(results).to_dict(),
        "cases": case_entries,
    }
    if extra:
        report.update(extra)
    return report


def write_report(report: dict[str, Any], path: str | Path) -> Path:
    """写报告；父目录自动创建。"""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return file_path


def read_report(path: str | Path) -> dict[str, Any]:
    """读回一份报告，结构/版本不对时抛出带路径的错误。

    基线对比只关心用例状态，但状态字段坏了会被 `_status_map` 静默当成 'None' 参与
    比较；版本不一致则可能把不同判分口径的报告混在一起。两种都必须在读入时挡住，
    并给出「是哪份文件」而不是一句 KeyError。
    """
    file_path = Path(path)
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"基线报告不存在：{file_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"基线报告不是合法 JSON（{file_path}）：{exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise ValueError(f"基线报告结构不正确（{file_path}）")

    version = payload.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"基线报告 schema_version={version!r}，当前工具是 {SCHEMA_VERSION}，不能直接对比（{file_path}）",
        )
    for index, entry in enumerate(payload["cases"], start=1):
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("id"), str)
            or entry.get("status") not in {"pass", "fail", "skip"}
        ):
            raise ValueError(f"基线报告第 {index} 条用例缺少合法的 id/status（{file_path}）")
    return payload


def select_baseline(
    spec: str,
    run_dir: str | Path,
    *,
    model: str,
    reasoning_effort: str | None,
    context_guard: str,
) -> Path | None:
    """解析 `--baseline`。

    - `none`：不做对比；
    - `auto`（默认）：在报告目录里取**最近一份行为配置完全一致**的报告。目录按模型
      分，但思考等级与上下文守卫是每次运行的参数，只看文件名会把不同配置的报告选成
      基线；
    - 其他值：原样当作路径，配置不一致由调用方警告（用户显式指定了它）。

    损坏/旧版本的报告在 auto 模式下跳过，而不是让整次评测失败——它们只是历史产物。
    """
    normalized = spec.strip().lower()
    if normalized == "none":
        return None
    if normalized != "auto":
        return Path(spec)

    for path in sorted(Path(run_dir).glob("*.json"), reverse=True):
        try:
            report = read_report(path)
        except ValueError:
            continue
        if (
            report.get("model") == model
            and report.get("reasoning_effort") == reasoning_effort
            and report.get("context_guard") == context_guard
        ):
            return path
    return None


def baseline_config_mismatch(
    report: dict[str, Any],
    *,
    model: str,
    reasoning_effort: str | None,
    context_guard: str,
) -> list[str]:
    """显式基线来自别的运行配置时返回差异说明（给人看的字符串）。"""
    expected: dict[str, Any] = {
        "model": model,
        "reasoning_effort": reasoning_effort,
        "context_guard": context_guard,
    }
    return [
        f"{key}：基线 {report.get(key)!r}，本次 {expected[key]!r}"
        for key in BEHAVIOR_FIELDS
        if report.get(key) != expected[key]
    ]


@dataclass(frozen=True)
class BaselineDiff:
    """两次运行的差异。

    真正要盯的只有 `newly_failed`：同一个用例从**非 FAIL** 变 FAIL，说明这次改动弄坏了
    某个已经能工作的行为。其余字段用于解释「为什么总数变了」。

    「非 FAIL」而不仅是「PASS」：用例此前因缺少依赖（例如没配 TAVILY_API_KEY）被 SKIP，
    这次依赖配上了、一跑就 FAIL——那同样是新出现的失败。此前只把 PASS→FAIL 算作回归，
    SKIP→FAIL 会掉进所有分类之外，命令行还会打印「行为与基线一致」，正好把最需要
    注意的一类变化藏起来。
    """

    newly_failed: tuple[str, ...] = ()
    newly_passed: tuple[str, ...] = ()
    still_failed: tuple[str, ...] = ()
    still_passed: tuple[str, ...] = ()
    newly_skipped: tuple[str, ...] = ()
    #: 仍然失败、但**失败原因变了**的用例（例如从「答错」变成「没用工具」）。
    #: 只报名字不够：修 A 弄坏 B 时，两次都红会让人以为「还是老问题」。
    changed_failures: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    context_tokens_delta: int = 0
    output_tokens_delta: int = 0
    latency_ms_delta: int = 0

    @property
    def has_regression(self) -> bool:
        return bool(self.newly_failed)

    @property
    def is_unchanged(self) -> bool:
        """完全没有变化：状态没变，且仍失败的用例连失败原因也没变。"""
        return not (
            self.newly_failed
            or self.newly_passed
            or self.newly_skipped
            or self.changed_failures
            or self.added
            or self.missing
        )


def _failure_checks(report: dict[str, Any]) -> dict[str, frozenset[str]]:
    """`{用例 id: 失败断言的集合}`。断言只认 check 名，不比对 detail 文案里的数值。"""
    checks: dict[str, frozenset[str]] = {}
    for entry in report.get("cases", []):
        if not isinstance(entry, dict):
            continue
        failures = entry.get("failures")
        names = {str(item.get("check")) for item in failures or [] if isinstance(item, dict)}
        checks[str(entry.get("id"))] = frozenset(names)
    return checks


def _status_map(report: dict[str, Any]) -> dict[str, str]:
    return {str(entry["id"]): str(entry["status"]) for entry in report.get("cases", [])}


def _total(report: dict[str, Any], key: str) -> int:
    value = report.get("totals", {}).get(key, 0)
    return int(value) if isinstance(value, (int, float)) else 0


def diff_reports(baseline: dict[str, Any], current: dict[str, Any]) -> BaselineDiff:
    """按用例 id 对比两次运行。缺失/新增的用例单独归类，不算回归。"""
    base = _status_map(baseline)
    cur = _status_map(current)

    common = [case_id for case_id in cur if case_id in base]
    # 按「本次状态是否变坏」分类，而不是只认 PASS↔FAIL：SKIP→PASS（依赖装上、行为其实
    # 早就对了）与 SKIP→FAIL（依赖装上、立刻暴露问题）都必须被归进去。
    newly_failed = tuple(sorted(i for i in common if cur[i] == "fail" and base[i] != "fail"))
    newly_passed = tuple(sorted(i for i in common if cur[i] == "pass" and base[i] != "pass"))
    still_failed = tuple(sorted(i for i in common if base[i] == "fail" and cur[i] == "fail"))
    still_passed = tuple(sorted(i for i in common if base[i] == "pass" and cur[i] == "pass"))
    newly_skipped = tuple(sorted(i for i in common if cur[i] == "skip" and base[i] != "skip"))
    added = tuple(sorted(i for i in cur if i not in base))
    missing = tuple(sorted(i for i in base if i not in cur))

    # 仍然失败但原因变了：只报名字会让人以为「还是老问题」，而修 A 弄坏 B 正是
    # 回归对比最想拦住的形态之一。缺 failures 字段的老报告不参与这条判定。
    base_checks = _failure_checks(baseline)
    cur_checks = _failure_checks(current)
    changed_failures = tuple(
        sorted(
            case_id
            for case_id in still_failed
            if case_id in base_checks and case_id in cur_checks and base_checks[case_id] != cur_checks[case_id]
        )
    )

    return BaselineDiff(
        newly_failed=newly_failed,
        newly_passed=newly_passed,
        still_failed=still_failed,
        still_passed=still_passed,
        newly_skipped=newly_skipped,
        changed_failures=changed_failures,
        added=added,
        missing=missing,
        context_tokens_delta=_total(current, "context_tokens") - _total(baseline, "context_tokens"),
        output_tokens_delta=_total(current, "output_tokens") - _total(baseline, "output_tokens"),
        latency_ms_delta=_total(current, "latency_ms") - _total(baseline, "latency_ms"),
    )
