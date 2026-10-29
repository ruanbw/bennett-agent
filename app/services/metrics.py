"""进程内指标：计数器与固定桶直方图，挂在 `/api/metrics` 上。

不引 prometheus_client（没有采集方）；多 worker 时每进程各一份，所以只是排障辅助而非计费依据。
标签值必须来自有限集合：URL/用户 id 这类高基数会变成几乎看不出来的内存泄漏。
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field

#: 延迟桶边界（秒）。上界取 600（`chat_service.TOTAL_STREAM_BUDGET_SECONDS`）：取小了长流全落进
#: `+Inf`，首字延迟的 p95 从此不可读；跨度按「大致翻倍」而非等距，延迟是长尾分布。
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
)

#: 单条指标最多保留多少个标签组合，超出后不再新增（并计数），避免高基数标签把内存吃光。
#: 它防的是「某个标签值其实是用户输入」这类实现失误。
MAX_SERIES_PER_METRIC = 200


@dataclass
class _Histogram:
    """一个标签组合下的延迟分布。"""

    buckets: tuple[float, ...]
    #: 每个桶的累计计数（Prometheus 口径：`le` 是「小于等于」的累计）。
    counts: list[int] = field(default_factory=list)
    total: int = 0
    sum_seconds: float = 0.0

    def __post_init__(self) -> None:
        if not self.counts:
            self.counts = [0] * (len(self.buckets) + 1)

    def observe(self, value: float) -> None:
        # 负值只可能来自时钟回拨：按 0 记，否则求和会变成负的、分位数全错
        seconds = max(0.0, value)
        index = len(self.buckets)
        for position, bound in enumerate(self.buckets):
            if seconds <= bound:
                index = position
                break
        self.counts[index] += 1
        self.total += 1
        self.sum_seconds += seconds


def _label_key(labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
    """把标签字典变成可哈希的键（顺序无关）。"""
    return tuple(sorted((str(name), str(value)) for name, value in labels.items()))


@dataclass
class MetricSample:
    """导出用的一条指标样本。"""

    name: str
    labels: dict[str, str]
    #: 计数器：总值。直方图：`None`（看 `buckets`）。
    value: float | None = None
    #: 直方图：`(上界, 累计计数)` 列表（最后一项上界为 `+Inf`）。
    buckets: list[tuple[float, int]] | None = None
    count: int = 0
    sum_seconds: float = 0.0


class MetricsRegistry:
    """计数器与直方图的容器。

    线程安全：同步端点跑在线程池里、SSE 生成器跑在事件循环里，两者可能同时写。写入频率
    相对业务量可以忽略，所以一把粗粒度锁换正确性。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[tuple[tuple[str, str], ...], float]] = defaultdict(dict)
        self._histograms: dict[str, dict[tuple[tuple[str, str], ...], _Histogram]] = defaultdict(dict)
        #: 因为超过 `MAX_SERIES_PER_METRIC` 被丢弃的样本数（按指标名）。
        self._dropped: dict[str, int] = defaultdict(int)

    def increment(self, name: str, amount: float = 1.0, **labels: str) -> None:
        """给计数器加 `amount`（默认 1）。"""
        key = _label_key(labels)
        with self._lock:
            series = self._counters[name]
            if key not in series and len(series) >= MAX_SERIES_PER_METRIC:
                self._dropped[name] += 1
                return
            series[key] = series.get(key, 0.0) + amount

    def observe(
        self,
        name: str,
        seconds: float,
        *,
        buckets: tuple[float, ...] = DEFAULT_BUCKETS,
        **labels: str,
    ) -> None:
        """记一次延迟观测（秒）。"""
        key = _label_key(labels)
        with self._lock:
            series = self._histograms[name]
            histogram = series.get(key)
            if histogram is None:
                if len(series) >= MAX_SERIES_PER_METRIC:
                    self._dropped[name] += 1
                    return
                histogram = _Histogram(buckets=buckets)
                series[key] = histogram
            histogram.observe(seconds)

    def counter_value(self, name: str, **labels: str) -> float:
        """读一个计数器的当前值（测试与内部判断用）。"""
        with self._lock:
            return self._counters.get(name, {}).get(_label_key(labels), 0.0)

    def reset(self) -> None:
        """清空全部指标。只用于测试：线上清空会让「错误率突增」这个信号消失。"""
        with self._lock:
            self._counters.clear()
            self._histograms.clear()
            self._dropped.clear()

    def snapshot(self) -> list[MetricSample]:
        """导出一份一致的快照（同一把锁内复制，避免边读边写导致分位数错位）。"""
        samples: list[MetricSample] = []
        with self._lock:
            for name, series in self._counters.items():
                for key, value in series.items():
                    samples.append(MetricSample(name=name, labels=dict(key), value=value))
            for name, series in self._histograms.items():
                for key, histogram in series.items():
                    cumulative: list[tuple[float, int]] = []
                    running = 0
                    # `counts[-1]` 是溢出桶（超过最后一个上界的样本），归进 +Inf。
                    for index, bound in enumerate(histogram.buckets):
                        running += histogram.counts[index]
                        cumulative.append((bound, running))
                    cumulative.append((float("inf"), histogram.total))
                    samples.append(
                        MetricSample(
                            name=name,
                            labels=dict(key),
                            buckets=cumulative,
                            count=histogram.total,
                            sum_seconds=histogram.sum_seconds,
                        )
                    )
        samples.sort(key=lambda sample: (sample.name, sorted(sample.labels.items())))
        return samples


#: 全局指标注册表。与 `app/tools/__init__.py` 的 `registry` 同一个套路：单例，
#: 由装配层与请求路径共用。
metrics = MetricsRegistry()


# ---------------------------------------------------------------------
# 指标名（集中在这里，避免同一个指标在两处拼出不同名字）
# ---------------------------------------------------------------------
#: HTTP 请求总数。标签：`method`、`route`、`status`（状态码的百位分类）。
HTTP_REQUESTS_TOTAL = "http_requests_total"

#: HTTP 请求耗时（秒）。标签同上。用路由模板而不是实际路径：`/api/x/123` 会让每个 id
#: 变成一个标签组合，直接吃光 `MAX_SERIES_PER_METRIC`。
HTTP_REQUEST_DURATION = "http_request_duration_seconds"

#: 对话流：完成/失败/中断。标签：`outcome`（done/error/stopped）。
CHAT_STREAMS_TOTAL = "chat_streams_total"

#: 对话流首字节耗时（秒）——用户感知的「卡不卡」主要就是这一段。
CHAT_FIRST_EVENT_DURATION = "chat_first_event_seconds"

#: 压缩。标签：`result`（compacted/rejected）。
CONTEXT_COMPACTIONS_TOTAL = "context_compactions_total"

#: 工具执行。标签：`tool`、`outcome`（success/failed）。
TOOL_EXECUTIONS_TOTAL = "tool_executions_total"

#: 备用模型切换。标签：`from`、`to`。
FALLBACKS_TOTAL = "model_fallbacks_total"


__all__ = [
    "CHAT_FIRST_EVENT_DURATION",
    "CHAT_STREAMS_TOTAL",
    "CONTEXT_COMPACTIONS_TOTAL",
    "DEFAULT_BUCKETS",
    "FALLBACKS_TOTAL",
    "HTTP_REQUESTS_TOTAL",
    "HTTP_REQUEST_DURATION",
    "MAX_SERIES_PER_METRIC",
    "MetricSample",
    "MetricsRegistry",
    "TOOL_EXECUTIONS_TOTAL",
    "metrics",
]
