"""上游模型错误的分类与用户文案：给用户看什么、要不要重试、要不要切备用模型。
两处判定必须共用这一份，否则「哪些错误可恢复」的理解会漂移；上游原文永不外泄。
"""

import asyncio
from collections.abc import Iterator

import httpx

#: 状态码 → 可外泄的中文文案。只映射能指导下一步动作的状态码，猜错细节比笼统更糟。
_STATUS_ERROR_MESSAGES: dict[int, str] = {
    400: "模型不可用，请更换模型",
    401: "上游认证配置问题，请联系管理员",
    403: "上游认证配置问题，请联系管理员",
    404: "模型不可用，请更换模型",
    429: "上游限流，请稍后重试",
}

SERVICE_UNAVAILABLE_MESSAGE = "服务暂时不可用，请重试"
GENERIC_STREAM_ERROR_MESSAGE = "生成异常，请重试"


def _group_members(exc: BaseException) -> tuple[BaseException, ...]:
    """取 `ExceptionGroup` 的成员。
    不能 `isinstance(exc, BaseExceptionGroup)`：3.10 上只有 `exceptiongroup` 回溯包，内建名不存在。
    """
    members = getattr(exc, "exceptions", None)
    return members if isinstance(members, tuple) else ()


def iter_exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """遍历异常链：`__cause__` / `__context__` 与 `ExceptionGroup` 的成员。
    备用模型全失败时状态码只在 `FallbackExceptionGroup` 的成员里，不展开就只剩通用文案。
    """
    seen: set[int] = set()

    def walk(current: BaseException | None) -> Iterator[BaseException]:
        if current is None or id(current) in seen:
            return
        seen.add(id(current))
        yield current
        for member in _group_members(current):
            yield from walk(member)
        yield from walk(current.__cause__ or current.__context__)

    yield from walk(exc)


def is_timeout_error(exc: BaseException) -> bool:
    """异常链（含异常组成员）里是否出现过超时。
    pydantic-ai 把连接类异常包装成没有 `status_code` 的 `ModelAPIError`，超时只留在 `__cause__` 链上。
    """
    return any(
        isinstance(item, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException))
        for item in iter_exception_chain(exc)
    )


def _status_code(exc: BaseException) -> int | None:
    """取 HTTP 状态码；拿不到或类型不对一律视为「没有」而不是猜。"""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return status
    return None


def is_retryable_upstream_error(exc: BaseException) -> bool:
    """是否值得立即用同一个模型重试一次：只认 5xx、超时、连接类错误。
    429 不在此列——再打一次只会加深限流，文案已经让用户稍后重试。
    """
    for item in iter_exception_chain(exc):
        status = _status_code(item)
        if status is not None and 500 <= status < 600:
            return True
        if isinstance(item, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException)):
            return True
        if isinstance(item, httpx.TransportError):
            return True
    return False


def is_fallback_eligible(exc: BaseException) -> bool:
    """是否值得换备用模型再试：比同模型重试宽一档，429 也切——限流可能只作用于当前模型。
    401/403 与 400/404 不切：同一套凭据下备用模型也会失败，或问题出在请求本身。
    """
    if any(_status_code(item) == 429 for item in iter_exception_chain(exc)):
        return True
    return is_retryable_upstream_error(exc)


def describe_fallback_reason(exc: BaseException) -> str:
    """把「为什么切到备用模型」压成一句短语，供事件、前端提示与导出使用。
    只给类别不给原始异常文本（可能含上游 URL 与凭据上下文）；429 必须先认，否则会被 5xx 分支漏掉。
    """
    chain = tuple(iter_exception_chain(exc))
    if any(_status_code(item) == 429 for item in chain):
        return "上游限流"
    for item in chain:
        status = _status_code(item)
        if status is not None and 500 <= status < 600:
            return f"上游 {status}"
    if any(isinstance(item, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException)) for item in chain):
        return "请求超时"
    if any(isinstance(item, httpx.TransportError) for item in chain):
        return "连接失败"
    return "上游错误"


#: 用户手动重试也不会成功的状态码（凭据/权限与请求本身的问题）：给重试按钮只会重复一次必然失败。
_NON_RETRYABLE_STATUSES = frozenset({400, 401, 403, 404})


def is_retryable_for_user(exc: BaseException) -> bool:
    """用户点「重试」是否可能成功（只决定 UI 是否展示按钮），比服务端自动重试更宽。
    只有明确的请求/凭据问题才返回 false——是否重试由用户决定。
    """
    for item in iter_exception_chain(exc):
        status = _status_code(item)
        if status is not None:
            return status not in _NON_RETRYABLE_STATUSES
    return True


def upstream_error_message(exc: BaseException) -> str:
    """把上游异常映射成可以推给用户的中文文案，只看状态码与「是否超时」。
    异常组按成员顺序取第一个带状态码的异常（通常就是主模型的真实失败原因）。
    """
    for item in iter_exception_chain(exc):
        status = _status_code(item)
        if status is None:
            continue
        if status in _STATUS_ERROR_MESSAGES:
            return _STATUS_ERROR_MESSAGES[status]
        if 500 <= status < 600:
            return SERVICE_UNAVAILABLE_MESSAGE

    if is_timeout_error(exc):
        return SERVICE_UNAVAILABLE_MESSAGE

    return GENERIC_STREAM_ERROR_MESSAGE
