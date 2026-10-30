"""LLM rubric judge：把 `judge="llm"` 的判分项一次性交给一个模型，逐项打 0-3 分。

为什么不逐项各发一次请求：N 次调用 = N 份方差 + N 倍延迟，而相邻判分项往往共享同一段
证据（同一句回答），拆开发只会让模型在不同请求里对同一段文字给出不同结论。

为什么返回结果要逐项校验、并且异常一律记 0：

- 判分器是整条链路里最不可信的一环。缺项如果按「不扣分」处理，判分器越坏分数越高；
- 上游超时/限流如果被当成「没有结论」而跳过，报告会出现「这一项没有分数但任务通过了」；
- 分值来自模型，可能越界（7、-1）或不是数字（"high"）。clamp 到 0-3 后归一化，
  非数字一律 0 分并写明原因——报告要能回答「这一项为什么是 0」。

判分项描述必须可判定（"回答中包含具体日期"而不是"回答得好不好"）：模型没有可判定的
标准时会给一个中庸分，而中庸分会让所有任务看起来都「差不多」，评测就失去分辨力了。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Protocol, runtime_checkable

import httpx

from quality.models import SCORE_SCALE, RubricItem, Score, Task, Trace

__all__ = [
    "DEFAULT_API_KEY",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "JudgeClient",
    "OpenJudgeClient",
    "RubricJudge",
    "parse_judge_payload",
]

#: 本地 OpenAI 兼容代理（与仓库其它评测脚本一致）。企业环境用环境变量覆盖。
DEFAULT_BASE_URL = "http://127.0.0.1:3425/v1"
DEFAULT_API_KEY = "magpie"
DEFAULT_MODEL = "group/deepseek-v4.1-flash"
DEFAULT_TIMEOUT_S = 120.0

SYSTEM_PROMPT = """你是严格的评测判分员。你只判下面列出的判分项，不评价文风，也不猜作者的意图。

每个判分项给一个 0-3 的整数分：
3 = 完全做到；2 = 做到但有可指出的瑕疵；1 = 只做到一部分；0 = 没做到或回答里没有证据。

规则：
- 回答里没有证据就判 0，不要给「宽厚的中间分」；
- 判分依据必须是回答或工具轨迹里的具体内容，rationale 里写你看到的那句证据；
- 只输出 JSON，不要输出任何解释性文字，不要用 markdown 代码块包裹。

输出格式（必须覆盖每一个判分项 id，不得新增 id）：
{"scores": {"<判分项id>": {"score": 0, "rationale": "一句话证据"}}}
"""


@runtime_checkable
class JudgeClient(Protocol):
    """判分模型的唯一接口。测试用 fake 实现，因此判分器本身完全离线可测。"""

    async def complete_json(self, system: str, user: str) -> dict[str, Any]:
        """返回已解析的 JSON 对象；无法解析时抛异常（由 `RubricJudge` 转成 0 分）。"""
        ...


def parse_judge_payload(content: str) -> dict[str, Any]:
    """解析模型返回的 JSON 对象。允许多包一层 markdown 代码块，其余一律报错。"""
    text = content.strip()
    if text.startswith("```"):
        text = "\n".join(line for line in text.splitlines() if not line.strip().startswith("```")).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"判分返回不是合法 JSON：{text[:200]!r}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"判分返回必须是 JSON 对象，收到 {type(payload).__name__}")
    return payload


def salvage_scores(content: str) -> dict[str, Any] | None:
    """从被截断的 JSON 里捞出已经完整的判分项。捞不到返回 None。

    只认 `"item_id": {…}` 这种结构：截断点之后的对象必然不完整，`raw_decode` 会失败，
    于是保留截断点之前的所有结论。这比「整段丢掉重来」便宜，也比「猜一个分值」诚实。
    """
    scores: dict[str, Any] = {}
    decoder = json.JSONDecoder()
    for match in re.finditer(r'"([a-z][a-z0-9_]*)"\s*:\s*(?=\{)', content):
        try:
            value, _ = decoder.raw_decode(content, match.end())
        except ValueError:
            continue
        if isinstance(value, dict):
            scores[match.group(1)] = value
    return {"scores": scores} if scores else None


class OpenJudgeClient:
    """打 OpenAI 兼容的 `/chat/completions`。``response_format=json_object`` 让模型少说废话。"""

    name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("QUALITY_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key or os.environ.get("QUALITY_API_KEY") or DEFAULT_API_KEY
        self.model = model or os.environ.get("QUALITY_MODEL") or DEFAULT_MODEL
        self.timeout_s = timeout_s
        # 只给测试用（httpx.MockTransport）：生产路径不注入，避免把拦截器悄悄带上线。
        self._transport = transport

    @property
    def endpoint(self) -> str:
        # 手拼完整 URL 而不用 base_url + 绝对路径：httpx 合并基址与路径时会丢掉 `/v1`，
        # 结果是 404 而不是报错，排查成本比手拼高得多。
        return f"{self.base_url}/chat/completions"

    def _payload(self, system: str, user: str) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            # 判分要可复现：同样的轨迹必须得到同样的分数，所以温度钉死 0。
            "temperature": 0,
            "response_format": {"type": "json_object"},
            # 不设上限时，推理型模型会把预算花在 reasoning 上，正文 JSON 被截断——
            # 截断的判分结果看起来像「模型乱答」，实际是预算不够（实测遇到过）。
            "max_tokens": 2048,
        }

    async def complete_json(self, system: str, user: str) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self.timeout_s, transport=self._transport) as client:
            response = await client.post(self.endpoint, json=self._payload(system, user), headers=headers)
            response.raise_for_status()
        body = response.json()
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"判分返回缺少 choices[0].message.content：{str(body)[:200]}") from exc
        if not isinstance(content, str):
            raise ValueError(f"判分返回的 content 不是字符串：{type(content).__name__}")
        try:
            return parse_judge_payload(content)
        except ValueError:
            # 截断是真实故障模式：整段 JSON 不合法，但前面几项已经完整输出。
            # 丢掉整段会让「判分器预算不够」变成「被测系统 0 分」。
            salvaged = salvage_scores(content)
            if salvaged is not None:
                return salvaged
            return await self._retry_strict(system, user, headers)

    async def _retry_strict(self, system: str, user: str, headers: dict[str, str]) -> dict[str, Any]:
        """解析失败后再要一次，并明确要求只输出 JSON。只重试一次：判分器不能变成重试风暴。"""
        payload = self._payload(system, user + "\n\n# 输出要求\n只输出一个 JSON 对象，不要解释、不要 markdown。")
        payload["max_tokens"] = 3072
        async with httpx.AsyncClient(timeout=self.timeout_s, transport=self._transport) as client:
            response = await client.post(self.endpoint, json=payload, headers=headers)
            response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        try:
            return parse_judge_payload(content)
        except ValueError:
            salvaged = salvage_scores(content)
            if salvaged is not None:
                return salvaged
            raise


def _score_index(payload: dict[str, Any]) -> dict[str, Any]:
    """把 `{"scores": {...}}` / `{"scores": [...]}` / 扁平映射统一成 `{item_id: 原始值}`。

    三种形态都是真实模型会给出的，其中列表形态最常见于「按判分项逐条列出」的提示词。
    不认识的形状按空处理（于是所有项都是缺项 → 0 分），不做猜测。
    """
    container = payload.get("scores", payload)
    if isinstance(container, dict):
        return {str(key): value for key, value in container.items()}
    if isinstance(container, list):
        index: dict[str, Any] = {}
        for entry in container:
            if not isinstance(entry, dict):
                continue
            key = entry.get("id") or entry.get("item_id")
            if isinstance(key, str):
                index[key] = entry
        return index
    return {}


def _zero(item: RubricItem, rationale: str) -> Score:
    return Score(item_id=item.id, dimension=item.dimension, value=0.0, rationale=rationale, source="llm")


def _score_one(item: RubricItem, raw: Any) -> Score:
    if raw is None:
        return _zero(item, "判分器未返回该项，按 0 计（缺项不能白拿分）")

    raw_value = raw.get("score", raw.get("value")) if isinstance(raw, dict) else raw
    rationale = ""
    if isinstance(raw, dict):
        rationale = str(raw.get("rationale") or raw.get("reason") or "").strip()

    # bool 是 int 的子类，`{"score": true}` 必须当非法值而不是 1 分。
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        return _zero(item, f"判分器返回了非法分值 {raw_value!r}，按 0 计：{rationale}".strip("："))

    clamped = min(SCORE_SCALE, max(0.0, float(raw_value)))
    if clamped != float(raw_value):
        rationale = f"分值 {raw_value} 越界，已 clamp 到 {clamped:g}/3：{rationale}".strip("：")
    return Score(
        item_id=item.id,
        dimension=item.dimension,
        value=clamped / SCORE_SCALE,
        rationale=rationale,
        source="llm",
    )


def _tool_trace(trace: Trace) -> str:
    """工具轨迹摘要。判「工具失败有没有如实说明」必须有工具结果，否则只剩模型自述。"""
    lines: list[str] = []
    for event in trace.events:
        if event.kind == "tool_call":
            args = json.dumps(event.payload.get("args", {}), ensure_ascii=False, sort_keys=True)
            lines.append(f"- 调用 {event.name}({args})")
        elif event.kind == "tool_result":
            result = str(event.payload.get("result", ""))
            ok = event.payload.get("ok", True)
            lines.append(f"- 结果 {event.name}（{'成功' if ok else '失败'}）：{result[:400]}")
    return "\n".join(lines) or "（本轮没有工具调用）"


def build_user_prompt(task: Task, trace: Trace, items: list[RubricItem]) -> str:
    """一次请求判完所有项：同一段回答的证据在同一上下文里，结论才不会自相矛盾。"""
    rubric_lines = "\n".join(f"- {item.id}（维度 {item.dimension}）：{item.description}" for item in items)
    return "\n\n".join(
        (
            f"# 任务提问\n{task.prompt}",
            f"# 判分项\n{rubric_lines}",
            f"# 工具轨迹\n{_tool_trace(trace)}",
            f"# 被测回答\n{trace.final_answer or '（空）'}",
        )
    )


class RubricJudge:
    """LLM 判分器。只负责 `judge="llm"` 的项，确定性项归 `ExpectationGrader`。"""

    id = "llm"

    def __init__(self, client: JudgeClient) -> None:
        self.client = client

    @staticmethod
    def _items(task: Task) -> list[RubricItem]:
        return [item for item in task.rubric if item.judge == "llm"]

    async def grade(self, task: Task, trace: Trace) -> list[Score]:
        items = self._items(task)
        if not items:
            return []
        if trace.error:
            # 没有回答就没有可判的内容。先短路，避免拿一份空回答去问模型（又贵又随机）。
            return [_zero(item, f"执行失败（{trace.error}），无内容可判") for item in items]

        try:
            payload = await self.client.complete_json(SYSTEM_PROMPT, build_user_prompt(task, trace, items))
        except Exception as exc:  # noqa: BLE001 - 判分器任何失败都必须落成 0 分，而不是把这一轮判崩
            return [_zero(item, f"judge_error: {type(exc).__name__}: {exc}") for item in items]

        index = _score_index(payload)
        return [_score_one(item, index.get(item.id)) for item in items]
