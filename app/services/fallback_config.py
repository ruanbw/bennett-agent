"""备用模型（`FALLBACK_MODEL_NAME`）的解析口径。

名字来自环境变量、能否用上却取决于库里的启用状态与提供商归属，`agent_factory` 与
`model_service.registry` 都调这里。「配了但用不上」必须留 WARNING：静默不兜底时用户直接看到失败。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from app.config import settings

logger = logging.getLogger(__name__)

#: 已经告警过的 `(主模型, 备用名)` 组合：避免每个请求都刷一行同样的 WARNING。
_warned: set[tuple[str, str | None]] = set()


def configured_fallback_name(primary_name: str) -> str | None:
    """配置里的备用模型名；空值、与主模型相同都视为「不启用」。

    同名没必要包 `FallbackModel`：只会对同一个模型再打一次，还把错误变成更绕的异常组。
    大小写不归一：模型 id 由网关定义，后端不做猜测。
    """
    fallback_name = (settings.fallback_model_name or "").strip()
    if not fallback_name or fallback_name == primary_name:
        return None
    return fallback_name


def reset_fallback_warnings() -> None:
    """清空告警去重集合（测试与「改了配置想再看一次告警」的场景用）。"""
    _warned.clear()


def warn_fallback_unavailable(primary_name: str, fallback_name: str, reason: str) -> None:
    """「配了备用模型但用不上」的告警，同一组合只记一次。

    `reason` 要具体到能指导动作（例如「同一提供商内没有这个启用模型」），不能是笼统的
    「备用模型不可用」。
    """
    key = (primary_name, fallback_name)
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(
        "备用模型已配置但本轮无法启用 (primary=%s, fallback=%s)：%s。"
        "主模型失败时不会切换，用户会直接看到失败。请检查 FALLBACK_MODEL_NAME 与后台的模型启用状态。",
        primary_name,
        fallback_name,
        reason,
    )


def find_fallback_model_id(
    candidate_ids: Iterable[str],
    fallback_name: str,
) -> str | None:
    """在候选模型 id 里找备用模型（纯函数，便于单测）。

    精确匹配：模型 id 由网关定义，`openai/gpt-4o` 与 `openai/GPT-4O` 是两个模型。
    """
    for candidate in candidate_ids:
        if candidate == fallback_name:
            return candidate
    return None
