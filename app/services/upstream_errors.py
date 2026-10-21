"""上游模型错误的分类与用户文案。

这里集中两类判定，避免它们各自漂移：

1. **给用户看什么**（`upstream_error_message`）——只依据状态码与「是否超时」，
   上游原文（响应体、URL、密钥上下文）永不外泄；
2. **要不要自动重试 / 要不要切备用模型**（`is_retryable_upstream_error` /
   `is_fallback_eligible`）——只认「过一会儿/换一个模型就可能成功」的失败。

之前这些逻辑都长在 `chat_service` 里；备用模型要复用同一套判定，若在
`agent_factory` 里再抄一份，两处对「哪些错误可恢复」的理解迟早会不一致。
"""

import asyncio
from collections.abc import Iterator

import httpx

#: 上游 HTTP 状态码 → 可外泄的中文文案。只映射「能指导用户下一步动作」的状态码，
#: 其余一律走通用文案：把上游细节猜错后写进用户可见消息，比笼统更糟。
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

    不能直接 `isinstance(exc, BaseExceptionGroup)`：项目支持 Python 3.10，那里只有
    `exceptiongroup` 回溯包，内建名并不存在。按结构取 `.exceptions` 对两者都成立。
    """
    members = getattr(exc, "exceptions", None)
    return members if isinstance(members, tuple) else ()


def iter_exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """遍历异常链：`__cause__` / `__context__`，以及 `ExceptionGroup` 的成员。

    备用模型（`FallbackModel`）在所有模型都失败时抛的是 `FallbackExceptionGroup`，
    真正的状态码/超时信息在成员里。不展开成员，分类就会退化成通用文案。
    用 `seen` 去重以防御环状链与成员/原因互相指向。
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

    pydantic-ai 会把 SDK 的连接类异常统一包装成 `ModelAPIError`（该类型**没有**
    `status_code`），超时信息只留在 `__cause__` 链上（openai.APITimeoutError →
    httpx.TimeoutException），所以这里按链找，而不是只看最外层类型。
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
    """判断一次失败是否值得**立即用同一个模型重试一次**。

    只认三类信号，宁可漏掉也不误重试：

    - 5xx：上游瞬时故障，换一次请求可能就成功；
    - 超时：空闲超时通常意味着连接/上游卡了一下；
    - 连接类错误（`httpx.TransportError`）：连接被重置、TLS 握手失败等。

    4xx 不重试（请求本身有问题，重试结果一样）；429 也不重试：上游限流时再打一次
    只会加深限流，文案已经让用户稍后重试。备用模型组的成员也参与判定：只要其中一个
    是 5xx/超时/连接错误，就说明「上游这一跳不健康」，值得再试一次。
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
    """判断一次失败是否值得**换备用模型**再试一次。

    比同模型重试宽一档：除了 5xx / 超时 / 连接错误，429 也可以切换——限流可能只
    作用于当前模型（或当前模型的上游配额），备用模型往往还有额度。

    401/403 不切换：同一套网关凭据下备用模型也会失败，只是白打一次请求；
    400/404 也不切换：那是请求或模型本身的问题，该把分类后的文案直接告诉用户。
    """
    if any(_status_code(item) == 429 for item in iter_exception_chain(exc)):
        return True
    return is_retryable_upstream_error(exc)


def describe_fallback_reason(exc: BaseException) -> str:
    """把「为什么切到备用模型」压成一句短语，给事件、前端提示与导出用。

    用户看到「已切换备用模型」时最自然的疑问是「为什么」；运维拿到一张截图也想知道
    是限流还是上游挂了。这里只给类别、不给原始异常文本——后者可能含上游 URL 与凭据上下文。

    判定顺序与 `is_fallback_eligible` 保持一致：429 先认（它同样是 4xx，容易被后面的
    分支漏掉），再 5xx、超时、连接错误。
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


#: 用户手动重试也不会成功的状态码：凭据/权限问题（401/403）与请求本身的问题
#: （400/404）。这些错误已经在文案里提示「换模型」或「联系管理员」，给重试按钮
#: 只会让用户重复一次必然失败的操作。
_NON_RETRYABLE_STATUSES = frozenset({400, 401, 403, 404})


def is_retryable_for_user(exc: BaseException) -> bool:
    """用户点「重试」是否可能成功（只用于 UI 是否展示重试按钮）。

    与 `is_retryable_upstream_error`（服务端自动重试）不同：这里更宽——429、超时、
    连接错误、未分类异常都算「值得让用户再试一次」，因为是否重试由用户决定；
    只有明确的请求/凭据问题才标 false。
    """
    for item in iter_exception_chain(exc):
        status = _status_code(item)
        if status is not None:
            return status not in _NON_RETRYABLE_STATUSES
    return True


def upstream_error_message(exc: BaseException) -> str:
    """把上游异常映射成可以推给用户的中文文案。

    只依据两个信号：HTTP 状态码（`ModelHTTPError.status_code`）与「是否超时」。
    状态码用安全取值：拿不到、类型不对、或不在映射表里都归入通用文案——上游原文
    可能带 URL、provider 上下文甚至密钥，永不外泄。

    异常组（备用模型全失败）按成员顺序取**第一个带状态码**的异常：它通常就是主模型
    失败的真实原因，比兜底模型附带的二次错误更能指导用户下一步动作。
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
