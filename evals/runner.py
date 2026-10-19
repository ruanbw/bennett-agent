"""评测执行器：把一条用例真正跑成 `TurnRecord`。

**走生产链路**（`stream_chat_events`）而不是直接 `agent.run()`：事件翻译、工具调用、
真实用量、备用模型切换、上下文预算都是被评测对象的一部分。少走一层，评测就会在
「服务端改了但 agent 没变」的回归上保持沉默。

为什么默认把 `CONTEXT_GUARD_MODE` 关掉：评测用例都是短输入，预算判定永远不会触发，
但打开时每个用例要多查一次模型窗口（数据库或网关列表），既拖慢一轮，也会让评测在
「网关列表一时取不到」时失败——那不是模型行为变差。用 `--context-guard` 可以打开。

本模块只被 `scripts/run_eval.py` 与测试导入；不参与服务运行。
"""

from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urlsplit, urlunsplit

from app.config import settings
from app.schemas.chat import ChatMessage
from app.schemas.events import (
    ClarificationRequest,
    ContentDelta,
    FallbackUsed,
    StreamError,
    ToolCall,
    Usage,
)
from app.services.chat_service import stream_chat_events
from app.services.model_service import ChatModelSnapshot
from evals.harness import EvalCase, ToolInvocation, TurnRecord

#: 单条用例的墙钟上限。评测是串行跑的，一条卡死会让整轮看起来像挂了；
#: 生产路径自身有 600s 总预算，这里再收一道，留出不拖太久的上界。
DEFAULT_CASE_TIMEOUT_SECONDS = 180.0


#: 评测走的是「环境变量网关」这条旧路径（`target=None`），因此不需要数据库：
#: 它测的是模型在给定提示词与工具集下的行为，与后台的提供商配置无关。
#: `registry_managed=False` 明确告诉 `stream_chat_events`：不要按数据库规则拒绝模型。
#: `known_ids=None`：不做白名单校验（评测的模型名就是 CLI/环境变量给的那个）。
def make_snapshot(model_key: str) -> ChatModelSnapshot:
    return ChatModelSnapshot(
        key=model_key,
        target=None,
        context_window=None,
        max_output_tokens=None,
        fallback_key=None,
        fallback_context_window=None,
        known_ids=None,
        registry_managed=False,
    )


def available_requirements() -> dict[str, bool]:
    """当前环境实际具备的外部能力，用于把对应用例标成 SKIP 而不是 FAIL。"""
    return {"tavily": bool(settings.tavily_api_key)}


def safe_endpoint(url: str) -> str:
    """报告里展示网关地址时去掉 userinfo。

    `LITELLM_API_BASE` 允许写成 `http://user:pass@host:4000`，报告会进 git 之外的
    文件、也可能被贴进 issue；用户名密码不该跟着走。
    """
    if "@" not in url:
        return url
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        return "<已隐藏凭据的地址>"
    netloc = parts.hostname
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _elapsed_ms(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)


async def run_case(
    case: EvalCase,
    *,
    model_key: str,
    reasoning_effort: str | None = None,
    timeout_seconds: float = DEFAULT_CASE_TIMEOUT_SECONDS,
) -> TurnRecord:
    """跑一条用例，永不抛异常：失败与超时都折叠成 `TurnRecord.error`。

    这样调用方只需要看记录，不需要为每条用例写一遍 try/except——「评测器自己崩了」
    与「模型答错了」在报告里必须能区分开，前者是 error 字段而不是一个空结果。
    """
    snapshot = make_snapshot(model_key)
    started_at = time.perf_counter()
    try:
        return await asyncio.wait_for(
            _consume(case, snapshot, reasoning_effort),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        return TurnRecord(
            error=f"单轮超过 {timeout_seconds:g}s 未完成",
            latency_ms=_elapsed_ms(started_at),
        )
    except Exception as exc:  # noqa: BLE001 - 评测器不能因为单条用例崩溃而中止整轮
        return TurnRecord(
            error=f"{type(exc).__name__}: {exc}",
            latency_ms=_elapsed_ms(started_at),
        )


async def _consume(
    case: EvalCase,
    snapshot: ChatModelSnapshot,
    reasoning_effort: str | None,
) -> TurnRecord:
    started_at = time.perf_counter()
    answer_parts: list[str] = []
    tools: list[ToolInvocation] = []
    clarification_options: tuple[str, ...] = ()
    context_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    fallback_to: str | None = None
    error: str | None = None

    messages = [ChatMessage(role="user", content=case.prompt)]

    async for event in stream_chat_events(snapshot, messages, reasoning_effort=reasoning_effort):
        if isinstance(event, ContentDelta):
            answer_parts.append(event.delta)
        elif isinstance(event, ToolCall):
            tools.append(ToolInvocation(name=event.tool_name, args=event.args))
        elif isinstance(event, ClarificationRequest):
            # 澄清工具在 chat_service 里被翻译成独立事件，不会以 ToolCall 出现。
            # 评测只关心「调没调、给了几个选项、问题是什么」，所以补一条记录。
            tools.append(
                ToolInvocation(
                    name="ask_user_clarification",
                    args=json.dumps(
                        {"question": event.question, "options": event.options},
                        ensure_ascii=False,
                    ),
                ),
            )
            if not clarification_options:
                clarification_options = tuple(event.options)
        elif isinstance(event, Usage):
            context_tokens = event.context_tokens
            output_tokens = event.output_tokens
            cache_read_tokens = event.cache_read_tokens
        elif isinstance(event, FallbackUsed):
            fallback_to = event.to_model
        elif isinstance(event, StreamError):
            error = event.message if event.code is None else f"{event.message}（{event.code}）"

    return TurnRecord(
        tools=tuple(tools),
        answer="".join(answer_parts),
        latency_ms=_elapsed_ms(started_at),
        context_tokens=context_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        fallback_to=fallback_to,
        error=error,
        clarification_options=clarification_options,
    )
