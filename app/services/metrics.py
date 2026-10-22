"""进程内指标：计数器与直方图，挂在 `/api/metrics` 上。

## 为什么不引 prometheus_client

本项目的部署形态是**单机 SQLite + 单进程**，没有 Prometheus 或任何时序库可接。引入
客户端只会多一个依赖和一套配置面，而拿到的东西（一个 `/metrics` 文本端点）在这里没有
采集方。真正需要的三件事是：

1. 现在这次是不是不正常（错误率、慢请求）；
2. 比以前慢了吗（延迟分位数）；
3. 上游/工具/压缩这些子系统各自的失败与耗时。

这三件事用「进程内计数器 + 固定的延迟桶」就能回答，代价约一百行。**边界必须写清楚**：
多 worker 部署时每个进程各有一份（与 `app/api/rate_limit.py` 的进程内限流同一个取舍），
所以它是**排障辅助**，不是计费或容量规划的依据。要精确聚合时，把 `snapshot()` 的返回值
推给真正的时序库即可，统计口径不用改。

## 为什么直方图用固定桶而不是存原始样本

存样本要无界内存（一次流量高峰就能吃掉几百 MB）。固定桶的内存是常数，而且足够回答
「p95 有没有变差」——精确到毫秒的分位数在排障里没有额外价值。

## 标签基数

每个标签组合都会占一份内存，因此**标签值必须来自有限集合**（状态码、模型名来自配置、
工具名来自注册表）。绝不把 URL、用户 id、query 这类高基数值放进去——那会变成内存泄漏，
而且慢到看不出来（这正是「指标把服务拖垮」的经典成因）。
"""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field

#: 延迟桶的边界（秒）。覆盖「本地缓存命中」到「上游 60 秒超时」的实际范围。
#:
#: 上界取 60 而不是 120：`TOTAL_STREAM_BUDGET_SECONDS` 是 120，但那一档的样本会被
#: `+Inf` 一并统计，多一个桶只会让输出更长。跨度用「大致翻倍」而不是等距——延迟分布
#: 是长尾的，等距桶会让大多数样本挤进第一个桶。
DEFAULT_BUCKETS: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)

#: 单条指标最多保留多少个标签组合。超出后不再新增（并计数），避免高基数标签把内存吃光。
#: 正常流量下远达不到这个量级；它防的是「某个标签值其实是用户输入」这类实现失误。
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

    线程安全：FastAPI 的同步端点跑在线程池里，而 SSE 生成器跑在事件循环里，两者可能
    同时写指标。用一把粗粒度的锁而不是无锁结构——写入频率相对业务量可以忽略，正确性
    比并发度重要得多。
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
        """清空全部指标。**只用于测试**：线上清空会让「错误率突增」这个信号消失。"""
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
                    # 前 len(buckets) 个桶，桶内的样本落在这里；`counts[-1]` 是溢出桶
                    # （超过最后一个上界的样本），归进 +Inf。
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

#: HTTP 请求耗时（秒）。标签同上。用**路由模板**而不是实际路径：`/api/x/123` 会让
#: 每个 id 变成一个标签组合，直接吃光 `MAX_SERIES_PER_METRIC`。
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
