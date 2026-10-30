"""LiveTarget：打真实实例的 `/api/chat`，把 SSE 事件翻成同一种 `Trace`。

它的价值只有一条：证明 Prompt Lab 的结论在真实链路上成立（数据库、限流、沙箱、
模型容灾全都参与）。所以它不做任何「为了通过而做的适配」——服务端发什么就记什么，
连不上就如实记 `live_unavailable`，不降级成离线结果冒充端到端。

鉴权走 Cookie：本服务的会话是 HttpOnly Cookie，`QUALITY_LIVE_COOKIE` 里直接放
`name=value` 对（多个用 `;` 分隔）。没有 Cookie 就没有会话，这是设计而不是限制。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, NamedTuple

import httpx

from app.schemas.events import ContentDelta, ThinkingDelta, ToolCall, ToolResult
from quality.models import Task, Trace, TraceEvent, fingerprint

DEFAULT_BASE_URL = os.environ.get("QUALITY_LIVE_BASE_URL", "http://127.0.0.1:8000")


class _StreamFailure(NamedTuple):
    """服务端在流内下发的 `type=="error"`。

    用本模块的内部类型回传给 `run`，而不是给 `TraceEvent` 加字段（`models.py` 是冻结契约）：
    `_parse_event` 的返回值只有 `run` 读，它需要的是「这一轮失败了」这个信号。
    """

    reason: str


#: `_parse_event` 的完成信号。只用来判断本轮是否真的收到了 `done`，不产生轨迹事件
#: （`done` 仍按未知事件记一条 guard，服务端说了什么要原样留证）。
_STREAM_DONE = object()


class LiveTarget:
    name = "live"

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        cookie: str | None = None,
        model: str | None = None,
        timeout_s: float = 600.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookie = cookie if cookie is not None else os.environ.get("QUALITY_LIVE_COOKIE", "")
        self.model = model
        self.timeout_s = timeout_s
        self._client = client

    def config_fingerprint(self) -> str:
        return fingerprint({"target": "live", "base_url": self.base_url, "model": self.model})

    async def run(self, task: Task, sample: int) -> Trace:
        started = time.monotonic()
        events: list[TraceEvent] = []
        answer = ""
        error: str | None = None

        def elapsed() -> int:
            return int((time.monotonic() - started) * 1000)

        body: dict[str, Any] = {"messages": [{"role": "user", "content": task.prompt}]}
        if self.model:
            body["model"] = self.model
        headers = {"Accept": "text/event-stream"}
        if self.cookie:
            headers["Cookie"] = self.cookie

        client = self._client or httpx.AsyncClient(timeout=self.timeout_s)
        owns_client = self._client is None
        try:
            async with client.stream(
                "POST", f"{self.base_url}/api/chat", json=body, headers=headers, timeout=self.timeout_s
            ) as response:
                if response.status_code >= 400:
                    error = f"live_http_{response.status_code}"
                    events.append(TraceEvent(kind="error", payload={"error": error}, at_ms=elapsed()))
                else:
                    saw_done = False
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if not payload:
                            continue
                        parsed = _parse_event(payload, elapsed, events)
                        if isinstance(parsed, ContentDelta):
                            answer += parsed.delta
                        elif isinstance(parsed, _StreamFailure):
                            # 第一次错误为准：后面就算来了 `done`，也不能把已经失败的一轮洗白。
                            error = error or parsed.reason
                        elif parsed is _STREAM_DONE:
                            saw_done = True
                    if error is None and not saw_done:
                        # 连接关了却没有 `done`：回答可能只有半截。不标记的话
                        # `answer_not_contains` 对半截/空回答恒真、gate 也不会命中，
                        # 探针会被记成「防住了」——ASR 0% 认证的是一个已经挂掉的系统。
                        error = "stream_incomplete"
                        events.append(TraceEvent(kind="error", payload={"error": error}, at_ms=elapsed()))
        except Exception as exc:  # 连不上/超时/中断都落成 Trace
            error = f"live_unavailable: {type(exc).__name__}: {exc}"
            events.append(TraceEvent(kind="error", payload={"error": error}, at_ms=elapsed()))
        finally:
            if owns_client:
                await client.aclose()

        return Trace(
            task_id=task.id,
            sample=sample,
            events=events,
            final_answer=answer,
            error=error,
            latency_ms=elapsed(),
            config_fingerprint=self.config_fingerprint(),
            canary=str(task.setup.get("canary") or "") or None,
        )


def _parse_event(raw: str, elapsed: Any, events: list[TraceEvent]) -> Any:
    """未知事件类型不丢弃：记为 `unknown_event`，否则服务端新增事件时评测会静默失明。"""
    try:
        payload = json.loads(raw)
    except ValueError:
        events.append(TraceEvent(kind="error", payload={"error": "bad_sse_json", "raw": raw[:500]}, at_ms=elapsed()))
        return None
    kind = payload.get("type")
    if kind == "content":
        event = ContentDelta.model_validate(payload)
        events.append(TraceEvent(kind="message", payload={"text": event.delta}, at_ms=elapsed()))
        return event
    if kind == "thinking":
        ThinkingDelta.model_validate(payload)
        return None
    if kind == "tool_call":
        call = ToolCall.model_validate(payload)
        events.append(
            TraceEvent(
                kind="tool_call",
                name=call.tool_name,
                payload={"args": call.args, "id": call.tool_call_id},
                at_ms=elapsed(),
            )
        )
        return call
    if kind == "tool_result":
        result = ToolResult.model_validate(payload)
        events.append(
            TraceEvent(
                kind="tool_result",
                name=result.tool_name,
                payload={"result": result.result, "ok": result.outcome == "success", "outcome": result.outcome},
                at_ms=elapsed(),
            )
        )
        return result
    if kind == "error":
        # 流内错误必须同时落成 `Trace.error`：只记一条 guard 事件的话，`run` 拿不到错误、
        # `final_answer` 留着半截文本，判分器会把它当成一份完整回答来判。
        message = str(payload.get("message") or "")
        reason = f"stream_error: {message}" if message else "stream_error"
        events.append(TraceEvent(kind="error", payload={"error": reason, "raw": payload}, at_ms=elapsed()))
        events.append(TraceEvent(kind="guard", payload={"event": kind, "raw": payload}, at_ms=elapsed()))
        return _StreamFailure(reason=reason)
    if kind == "done":
        events.append(TraceEvent(kind="guard", payload={"event": kind, "raw": payload}, at_ms=elapsed()))
        return _STREAM_DONE
    events.append(TraceEvent(kind="guard", payload={"event": kind, "raw": payload}, at_ms=elapsed()))
    return None
