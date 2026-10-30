"""provider 契约的两条硬边界：容量判定不看错误文案、单次执行上限必须小于整轮预算。

零基础设施：SDK 的 `Sandbox.create` 换成替身，一个请求都不发。这两条都是「改错也照样跑、
只有出事故时才看得见」的边界，所以用断言钉住而不是靠注释。
"""

from __future__ import annotations

from typing import Any

import pytest
from opensandbox.exceptions import SandboxApiException, SandboxRateLimitException

from app.services.chat_service import TOTAL_STREAM_BUDGET_SECONDS
from app.services.sandbox import MAX_EXEC_TIMEOUT_S
from app.services.sandbox import opensandbox as provider_module
from app.services.sandbox.base import SandboxCapacityError, SandboxError, SandboxSpec
from app.services.sandbox.opensandbox import OpenSandboxProvider
from app.tools import SANDBOX_TOOL_TIMEOUT_SECONDS

SPEC = SandboxSpec(
    lease_id="sbx_00000000000000000000000000000000",
    template="bennett-sandbox",
    template_version="test",
    owner_tag="deadbeefdeadbeef",
    cpu=1.0,
    memory_mb=512,
)

#: 不连任何真实服务：`_config()` 只把这个地址塞进 `ConnectionConfig`，而 `create` 已被替身接管。
SERVER_URL = "http://sandbox.invalid"


def _fail_create_with(monkeypatch, error: Exception) -> None:
    """让 `Sandbox.create` 抛指定异常（建机路径上唯一有分支的地方就是它）。"""

    class _FailingSandbox:
        @staticmethod
        async def create(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401, ARG005
            raise error

    monkeypatch.setattr(provider_module, "Sandbox", _FailingSandbox)


async def _create(monkeypatch, error: Exception) -> None:
    _fail_create_with(monkeypatch, error)
    await OpenSandboxProvider(base_url=SERVER_URL).create(SPEC)


# ---------------------------------------------------------------------
# ① 容量不足按结构化字段判，不靠文案
# ---------------------------------------------------------------------
async def test_rate_limit_exception_is_reported_as_capacity(monkeypatch):
    """SDK 对 429 抛 `SandboxRateLimitException`：类型本身就是「池满」的证据。"""
    with pytest.raises(SandboxCapacityError):
        await _create(monkeypatch, SandboxRateLimitException("too many requests"))


async def test_429_status_is_capacity_even_without_the_rate_limit_type(monkeypatch):
    """只看类型不够：SDK 的不同版本/网关可能只给 `status_code=429`。"""
    with pytest.raises(SandboxCapacityError):
        await _create(monkeypatch, SandboxApiException("server said no", status_code=429))


async def test_capacity_wording_is_still_recognized(monkeypatch):
    """兜底子串：部分网关把「no capacity」包成 500 + 纯文本 body，那时只剩文案可看。"""
    with pytest.raises(SandboxCapacityError):
        await _create(monkeypatch, SandboxApiException("create failed: no capacity left", status_code=500))


async def test_other_create_failures_are_not_reported_as_capacity(monkeypatch):
    """非容量失败必须保持「执行环境暂时不可用」：说成池满会让模型以为等一等就好。"""
    with pytest.raises(SandboxError) as exc_info:
        await _create(monkeypatch, SandboxApiException("image pull failed", status_code=500))

    assert not isinstance(exc_info.value, SandboxCapacityError)


# ---------------------------------------------------------------------
# ② 单次执行上限 < 整轮预算
# ---------------------------------------------------------------------
def test_exec_timeout_stays_below_the_round_budget():
    """一轮对话总共只有 `TOTAL_STREAM_BUDGET_SECONDS`，比它更长的「上限」是不可达的假承诺。

    超预算时流式先抛 `StreamTimeoutError`，模型看到的是一句「生成超时」，工具里的中文建议
    一句都到不了，而容器里的进程还在跑。
    """
    assert MAX_EXEC_TIMEOUT_S < TOTAL_STREAM_BUDGET_SECONDS
    # 外层防线要大于内部上限（否则内部文案不可达），同时也在整轮预算之内（否则整轮先中止）。
    assert MAX_EXEC_TIMEOUT_S < SANDBOX_TOOL_TIMEOUT_SECONDS < TOTAL_STREAM_BUDGET_SECONDS
