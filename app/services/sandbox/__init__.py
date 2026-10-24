"""沙箱子系统的入口：provider 装配点。

## 为什么 provider 通过函数取，而不是模块级单例

工具函数在 import 期就被注册进 `registry`，但 provider 的构造依赖配置（`SANDBOX_ENABLED`
可能关着、`e2b` 后端需要 token）。在 import 期构造 provider 会让「配置不全」变成
「整个应用起不来」，而沙箱是可选能力——关掉它不该影响对话、RAG、同步。

`get_provider()` 因此是**惰性 + 带缓存**的：第一次真正要建沙箱时才构造，
服务端连不上也只在那时暴露。

## 测试怎么注入

`set_provider_for_tests()` 覆盖缓存。工具层只认契约（`SandboxProvider`），
所以越权、截断、生命周期这些关键行为可以用假 provider 验证，不需要 Docker/KVM。
"""

from __future__ import annotations

from app.config import settings
from app.services.sandbox.base import (
    DEFAULT_EXEC_TIMEOUT_S,
    MAX_EXEC_TIMEOUT_S,
    MAX_OUTPUT_HEAD_CHARS,
    MAX_OUTPUT_TAIL_CHARS,
    ExecResult,
    SandboxCapacityError,
    SandboxError,
    SandboxHandle,
    SandboxNotFound,
    SandboxProvider,
    SandboxSpec,
    SandboxTimeout,
    owner_tag,
)

_PROVIDER: SandboxProvider | None = None


def is_sandbox_enabled() -> bool:
    """沙箱是否可用。**唯一**的开关判据，工具装配与能力探针都读它。"""
    return bool(settings.sandbox_enabled)


def build_provider() -> SandboxProvider:
    """按配置构造 provider。配置不合法时抛 `SandboxError`（不是 ValueError——

    工具层会把它翻译成给模型看的中文，而 `ValueError` 的英文消息会原样漏进对话）。
    """
    backend = settings.sandbox_provider
    if backend == "opensandbox":
        from app.services.sandbox.opensandbox import OpenSandboxProvider

        return OpenSandboxProvider()
    raise SandboxError(f"未知的沙箱后端：{backend}")


def get_provider() -> SandboxProvider:
    """取当前 provider（惰性构造并缓存）。"""
    global _PROVIDER
    if _PROVIDER is None:
        _PROVIDER = build_provider()
    return _PROVIDER


def set_provider_for_tests(provider: SandboxProvider | None) -> None:
    """覆盖/清空 provider 缓存。只给测试用。"""
    global _PROVIDER
    _PROVIDER = provider


__all__ = [
    "DEFAULT_EXEC_TIMEOUT_S",
    "MAX_EXEC_TIMEOUT_S",
    "MAX_OUTPUT_HEAD_CHARS",
    "MAX_OUTPUT_TAIL_CHARS",
    "ExecResult",
    "SandboxCapacityError",
    "SandboxError",
    "SandboxHandle",
    "SandboxNotFound",
    "SandboxProvider",
    "SandboxSpec",
    "SandboxTimeout",
    "build_provider",
    "get_provider",
    "is_sandbox_enabled",
    "owner_tag",
    "set_provider_for_tests",
]
