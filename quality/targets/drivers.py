"""模型驱动：把「模型怎么回」与「提示词/工具怎么给」解耦。

- `OpenAICompatDriver`：打任何 OpenAI 兼容 `/chat/completions`（含本地 LiteLLM 代理）。
  这是评测出真数字的路径。
- `ScriptedDriver`：按用例 `setup.script` 回放确定的动作序列。只用于测试评测框架自身，
  它测不出模型好坏——报告里会把 driver 名写进指纹，避免把回放分数当成模型分数。

两者都不做重试：重试是执行器的事。驱动层偷偷重试会让「延迟」与「失败率」两个指标同时失真。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from quality.targets.tools import TOOL_SPECS, ToolRuntime

#: 默认指向本机 LiteLLM 兼容代理；换真实厂商只需改环境变量。
DEFAULT_BASE_URL = os.environ.get("QUALITY_BASE_URL", "http://127.0.0.1:3425/v1")
DEFAULT_API_KEY = os.environ.get("QUALITY_API_KEY", "magpie")
DEFAULT_MODEL = os.environ.get("QUALITY_MODEL", "group/deepseek-v4.1-flash")
DEFAULT_TIMEOUT_S = float(os.environ.get("QUALITY_TIMEOUT_S", "300"))


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    id: str = ""


@dataclass
class ModelReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    #: 上游 finish_reason，写进 Trace 便于解释「为什么没有最终回答」。
    finish_reason: str = ""


class ModelDriver(Protocol):
    name: str

    async def reply(self, *, system: str, messages: list[dict[str, Any]]) -> ModelReply: ...


def _usage_tokens(payload: dict[str, Any]) -> tuple[int, int]:
    usage = payload.get("usage") or {}
    return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)


class OpenAICompatDriver:
    """OpenAI 兼容 chat completions + function calling。

    思考等级显式关掉：评测要比较的是「同一配置下提示词改动带来的差异」，
    让 reasoning effort 在样本间漂移会把方差记到提示词头上。
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str = DEFAULT_API_KEY,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        temperature: float = 0.0,
        extra_body: dict[str, Any] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = f"openai-compat:{model}"
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.temperature = temperature
        self.extra_body = extra_body or {}
        self._client = client

    async def reply(self, *, system: str, messages: list[dict[str, Any]]) -> ModelReply:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "tools": [spec.as_openai() for spec in TOOL_SPECS],
            "tool_choice": "auto",
            "temperature": self.temperature,
            **self.extra_body,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        client = self._client or httpx.AsyncClient(timeout=self.timeout_s)
        owns_client = self._client is None
        try:
            response = await client.post(
                f"{self.base_url}/chat/completions", json=body, headers=headers, timeout=self.timeout_s
            )
            response.raise_for_status()
            payload = response.json()
        finally:
            if owns_client:
                await client.aclose()

        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            calls.append(
                ToolCall(
                    name=str(function.get("name") or ""),
                    args=_parse_args(function.get("arguments")),
                    id=str(raw.get("id") or ""),
                )
            )
        input_tokens, output_tokens = _usage_tokens(payload)
        return ModelReply(
            text=str(message.get("content") or ""),
            tool_calls=calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            finish_reason=str(choice.get("finish_reason") or ""),
        )


def _parse_args(raw: Any) -> dict[str, Any]:
    """模型给的 arguments 是字符串，也可能是已经是对象；坏 JSON 不能炸掉整轮，
    交给工具层报「参数不合法」——那正是「模型会不会看报错自救」要被测的行为。"""
    import json

    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {"__invalid_json__": str(raw)[:500]}
    return parsed if isinstance(parsed, dict) else {"__invalid_json__": str(raw)[:500]}


class ScriptedDriver:
    """按 `setup.script` 回放：`[{"call": "tool", "args": {...}}, ..., {"say": "最终回答"}]`。

    没有 script 时返回一条显式失败文案而不是编一个像样的回答：静默编造会让
    「框架自测」看起来像「模型表现」。
    """

    name = "scripted"

    def __init__(self, script: list[dict[str, Any]] | None = None) -> None:
        self._script = list(script or [])
        self._cursor = 0
        self._runtime = ToolRuntime()

    async def reply(self, *, system: str, messages: list[dict[str, Any]]) -> ModelReply:
        if self._cursor >= len(self._script):
            if not self._script:
                return ModelReply(
                    text="[scripted] 未提供脚本：该用例只能用 --driver openai 评测。",
                    finish_reason="scripted_missing",
                )
            return ModelReply(text=self._final_text(), finish_reason="stop")
        step = self._script[self._cursor]
        self._cursor += 1
        if "call" in step:
            return ModelReply(
                tool_calls=[
                    ToolCall(
                        name=str(step["call"]),
                        args=dict(step.get("args") or {}),
                        id=f"call_{self._cursor}",
                    )
                ],
                input_tokens=int(step.get("input_tokens") or 0),
                output_tokens=int(step.get("output_tokens") or 0),
                finish_reason="tool_calls",
            )
        return ModelReply(
            text=str(step.get("say") or ""),
            input_tokens=int(step.get("input_tokens") or 0),
            output_tokens=int(step.get("output_tokens") or 0),
            finish_reason="stop",
        )

    def _final_text(self) -> str:
        for step in reversed(self._script):
            if "say" in step:
                return str(step["say"])
        return "[scripted] 脚本已执行完，没有 say 步骤。"


def build_driver(config: dict[str, Any]) -> ModelDriver:
    """按运行配置造驱动。`kind=scripted` 必须显式给出，默认走真实模型。"""
    kind = str(config.get("driver") or "openai").lower()
    if kind == "scripted":
        return ScriptedDriver(config.get("script"))
    if kind != "openai":
        raise ValueError(f"未知 driver: {kind}（可选 openai / scripted）")
    return OpenAICompatDriver(
        model=str(config.get("model") or DEFAULT_MODEL),
        base_url=str(config.get("base_url") or DEFAULT_BASE_URL),
        api_key=str(config.get("api_key") or DEFAULT_API_KEY),
        timeout_s=float(config.get("timeout_s") or DEFAULT_TIMEOUT_S),
        temperature=float(config.get("temperature") or 0.0),
        extra_body=dict(config.get("extra_body") or {}),
    )
