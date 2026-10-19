"""上游错误分类：用户文案、同模型重试、备用模型切换。

三件事必须共用同一套判定，否则会出现「重试了但文案说不能重试」这类漂移。
备用模型全失败时抛的是 `FallbackExceptionGroup`，状态码在成员里——这也是本文件
重点锁住的边界。
"""

import httpx
import pytest
from pydantic_ai.exceptions import (
    FallbackExceptionGroup,
    ModelAPIError,
    ModelHTTPError,
)

from app.services.upstream_errors import (
    GENERIC_STREAM_ERROR_MESSAGE,
    SERVICE_UNAVAILABLE_MESSAGE,
    is_fallback_eligible,
    is_retryable_upstream_error,
    upstream_error_message,
)


def http_error(status):
    return ModelHTTPError(status_code=status, model_name="openai/gpt-4o")


def wrapped(message="boom"):
    return ModelAPIError(model_name="openai/gpt-4o", message=message)


# ---------------------------------------------------------------------
# 异常组（备用模型全失败）
# ---------------------------------------------------------------------
def test_fallback_group_message_uses_the_first_status():
    """主模型 503 + 备用模型 401 时，向用户解释主模型失败的真实原因。"""
    group = FallbackExceptionGroup("all models failed", [http_error(503), http_error(401)])

    assert upstream_error_message(group) == SERVICE_UNAVAILABLE_MESSAGE


def test_fallback_group_falls_through_to_the_first_classified_status():
    group = FallbackExceptionGroup("all models failed", [wrapped(), http_error(401)])

    assert upstream_error_message(group) == "上游认证配置问题，请联系管理员"


def test_fallback_group_without_classification_uses_the_generic_message():
    group = FallbackExceptionGroup("all models failed", [RuntimeError("a"), RuntimeError("b")])

    assert upstream_error_message(group) == GENERIC_STREAM_ERROR_MESSAGE


def test_fallback_group_members_drive_retry_eligibility():
    """组里只要有一个 5xx，就说明上游这一跳不健康，值得整轮再试一次。"""
    group = FallbackExceptionGroup("all models failed", [http_error(503), http_error(401)])

    assert is_retryable_upstream_error(group)
    assert is_fallback_eligible(group)


# ---------------------------------------------------------------------
# 分类
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "status,expected",
    [
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (429, True),
        (500, True),
        (503, True),
    ],
)
def test_fallback_eligibility(status, expected):
    """429 可以换模型（限流可能只作用于当前模型）；401/403/400/404 不换。"""
    assert is_fallback_eligible(http_error(status)) is expected


@pytest.mark.parametrize("status,expected", [(400, False), (429, False), (500, True), (502, True)])
def test_same_model_retry_eligibility(status, expected):
    """同模型重试不含 429：立刻再打一次只会加深限流。"""
    assert is_retryable_upstream_error(http_error(status)) is expected


def test_timeout_is_retryable_and_fallback_eligible():
    error = wrapped("request timed out")
    error.__cause__ = httpx.ReadTimeout("timed out")

    assert is_retryable_upstream_error(error)
    assert is_fallback_eligible(error)


def test_connection_error_is_retryable_and_fallback_eligible():
    error = wrapped("connection reset")
    error.__cause__ = httpx.ConnectError("connection refused")

    assert is_retryable_upstream_error(error)
    assert is_fallback_eligible(error)
