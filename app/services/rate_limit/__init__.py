"""限流的对外出口：api 层只留 HTTP 装配，算法与状态按「变化原因」拆到子模块。

拆包边界：`limiter` 是速率维度的限流器本体（进程内滑动窗口、Redis 共享窗口、装配工厂），
`client_ip` 把请求来源还原并归一化成限流桶键，`inflight` 管并发维度（在途对话流槽位）。
速率与并发是两个维度，必须分开限制（同 LiteLLM 的 RPM 与 `max_parallel_requests` 并列）；
搬家的边界条件是键格式、降级语义与错误码一个都不变，`app/api/rate_limit.py` 原样 re-export
这里的公开符号，既有调用方的 import 路径不动。
"""

from app.services.rate_limit.client_ip import (
    bucket_ip_address,
    client_key,
    parse_trusted_proxy_networks,
    resolve_client_ip,
)
from app.services.rate_limit.inflight import (
    CHAT_MAX_INFLIGHT_STREAMS,
    INFLIGHT_RETRY_AFTER_SECONDS,
    INFLIGHT_STREAM_TTL_SECONDS,
    InFlightStreamLimiter,
    InFlightStreamLimitExceeded,
    get_inflight_limiter,
    inflight_stream_slot,
)
from app.services.rate_limit.limiter import (
    RATE_LIMIT_WINDOW_SECONDS,
    RedisSlidingWindowLimiter,
    SlidingWindowLimiter,
    limiter_with,
    make_limiter,
)

__all__ = [
    "CHAT_MAX_INFLIGHT_STREAMS",
    "INFLIGHT_RETRY_AFTER_SECONDS",
    "INFLIGHT_STREAM_TTL_SECONDS",
    "RATE_LIMIT_WINDOW_SECONDS",
    "InFlightStreamLimitExceeded",
    "InFlightStreamLimiter",
    "RedisSlidingWindowLimiter",
    "SlidingWindowLimiter",
    "bucket_ip_address",
    "client_key",
    "get_inflight_limiter",
    "inflight_stream_slot",
    "limiter_with",
    "make_limiter",
    "parse_trusted_proxy_networks",
    "resolve_client_ip",
]
