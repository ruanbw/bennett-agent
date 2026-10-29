"""沙箱子系统的入口：provider 装配点。

provider 惰性构造：import 期构造会让「配置不全」变成整个应用起不来，而沙箱只是可选能力。
工具层只认 `SandboxProvider` 契约，测试用 `set_provider_for_tests()` 注入假实现，不需要 Docker。
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
    """按配置构造 provider；配置不合法时抛 `SandboxError` 而不是 `ValueError`。

    工具层会把 `SandboxError` 翻译成给模型看的中文，`ValueError` 的英文消息会原样漏进对话。
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
