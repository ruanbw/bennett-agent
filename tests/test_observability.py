"""可观测性的行为锁。

三条东西要钉住：

1. **请求 ID 的传播**：它必须同时出现在响应头与该请求的全部日志行里，否则「报障
   时给个编号」这件事在运维侧接不上；
2. **指标口径**：错误率必须能被算出来（成功的轮次与失败的轮次落在同一组标签下），
   且标签基数有界（否则指标自己会变成内存泄漏）；
3. **健康检查的两层语义**：liveness 不碰数据库（否则数据库抖动会触发重启风暴），
   readiness 必须碰（否则进程假活着一直在 500）。
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.observability import (
    RequestIdFilter,
    new_request_id,
    sanitize_request_id,
)
from app.services.metrics import MAX_SERIES_PER_METRIC, MetricsRegistry


# ---------------------------------------------------------------------
# 请求 ID
# ---------------------------------------------------------------------
def test_new_request_id_is_short_hex():
    request_id = new_request_id()

    assert len(request_id) == 12
    assert all(char in "0123456789abcdef" for char in request_id)


def test_two_request_ids_differ():
    assert new_request_id() != new_request_id()


@pytest.mark.parametrize(
    "raw",
    ["abc-123", "req.42_x", "A" * 64],
)
def test_valid_incoming_request_id_is_kept(raw):
    """网关/客户端已经带了 ID 时不再生成新的：链路里应当只有一个 ID。"""
    assert sanitize_request_id(raw) == raw


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "A" * 65,  # 超长
        "bad\nvalue",  # 换行会伪造一条日志行
        "带中文",  # 非 ASCII
        "has space",
    ],
)
def test_invalid_incoming_request_id_is_rejected(raw):
    assert sanitize_request_id(raw) is None


def test_filter_fills_placeholder_outside_request():
    """不在请求上下文里时给 `-` 而不是空串：日志格式的空缺会让列错位。"""
    record = logging.LogRecord(
        name="t", level=logging.INFO, pathname=__file__, lineno=1, msg="m", args=(), exc_info=None
    )

    RequestIdFilter().filter(record)

    assert record.request_id == "-"


def test_filter_uses_context_value_inside_request():
    token = _set_request_id("abc123")
    try:
        record = logging.LogRecord(
            name="t", level=logging.INFO, pathname=__file__, lineno=1, msg="m", args=(), exc_info=None
        )
        RequestIdFilter().filter(record)

        assert record.request_id == "abc123"
    finally:
        _reset_request_id(token)


def _set_request_id(value: str):  # noqa: ANN202
    from app.observability import _request_id

    return _request_id.set(value)


def _reset_request_id(token) -> None:  # noqa: ANN001
    from app.observability import _request_id

    _request_id.reset(token)


# ---------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------
def test_counter_accumulates_per_label_set():
    registry = MetricsRegistry()

    registry.increment("t_total", model="a")
    registry.increment("t_total", model="a", amount=2)
    registry.increment("t_total", model="b")

    assert registry.counter_value("t_total", model="a") == 3.0
    assert registry.counter_value("t_total", model="b") == 1.0
    assert registry.counter_value("t_total", model="c") == 0.0


def test_label_order_does_not_create_new_series():
    """标签顺序不同必须是同一条序列，否则每个调用点的关键字顺序都会凭空多一条。"""
    registry = MetricsRegistry()

    registry.increment("t_total", a="1", b="2")
    registry.increment("t_total", b="2", a="1")

    samples = registry.snapshot()
    assert len(samples) == 1
    assert samples[0].value == 2.0


def test_histogram_buckets_are_cumulative():
    """Prometheus 的 `le` 是累计语义；写成非累计，分位数会算错。"""
    registry = MetricsRegistry()
    for value in (0.001, 0.05, 0.3, 5.0):
        registry.observe("d_seconds", value)

    sample = registry.snapshot()[0]

    bounds = {bound: count for bound, count in sample.buckets}
    assert bounds[0.005] == 1  # 只有 0.001 落在这里
    assert bounds[0.1] == 2  # 累计：0.001 + 0.05
    assert bounds[1.0] == 3
    assert bounds[float("inf")] == 4
    assert sample.count == 4


def test_histogram_overflow_goes_to_inf_bucket():
    registry = MetricsRegistry()
    registry.observe("d_seconds", 999.0)

    sample = registry.snapshot()[0]

    assert sample.buckets[-1] == (float("inf"), 1)
    assert sample.count == 1


def test_negative_observation_is_clamped():
    """时钟回拨能产生负耗时；记成负数会让求和与分位数全错。"""
    registry = MetricsRegistry()
    registry.observe("d_seconds", -1.0)

    sample = registry.snapshot()[0]
    assert sample.sum_seconds == 0.0
    assert sample.count == 1


def test_series_cardinality_is_capped():
    """高基数标签会变成内存泄漏——上限必须真的生效，而不是只在注释里。"""
    registry = MetricsRegistry()
    for index in range(MAX_SERIES_PER_METRIC + 20):
        registry.increment("t_total", id=str(index))

    assert len(registry.snapshot()) == MAX_SERIES_PER_METRIC


def test_reset_clears_everything():
    registry = MetricsRegistry()
    registry.increment("t_total", a="1")
    registry.observe("d_seconds", 0.1)

    registry.reset()

    assert registry.snapshot() == []


def test_snapshot_is_sorted_for_stable_output():
    """输出顺序稳定，断言（与 diff 审阅）才有意义。"""
    registry = MetricsRegistry()
    registry.increment("b_total", x="1")
    registry.increment("a_total", x="1")

    assert [sample.name for sample in registry.snapshot()] == ["a_total", "b_total"]


# ---------------------------------------------------------------------
# 运维端点
# ---------------------------------------------------------------------
@pytest.fixture
def ops_client():
    from app.api.ops import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_health_does_not_touch_the_database(monkeypatch, ops_client):
    """liveness 不能碰数据库：数据库抖动时触发重启只会把在途请求一起丢掉。"""
    from app.api import ops

    class _ExplodingEngine:
        """一旦被访问就炸——`/health` 不该碰数据库。"""

        def __getattr__(self, name):  # noqa: ANN001
            raise AssertionError(f"liveness 不该访问数据库（用到了 engine.{name}）")

    monkeypatch.setattr(ops, "engine", _ExplodingEngine())

    response = ops_client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_reports_version(ops_client):
    response = ops_client.get("/health")

    assert response.json()["version"]


def test_metrics_endpoint_is_hidden_by_default(monkeypatch, ops_client):
    """默认关闭：多开一个免认证端点就多一分侦察面。

    返回 404 而不是 403——「不存在」比「存在但你不能看」泄露的信息更少。
    """
    monkeypatch.setattr(settings, "metrics_enabled", False)

    assert ops_client.get("/api/metrics").status_code == 404


def test_metrics_endpoint_renders_prometheus_text(monkeypatch, ops_client):
    from app.services.metrics import metrics

    monkeypatch.setattr(settings, "metrics_enabled", True)
    metrics.reset()
    metrics.increment("test_counter_total", route="/api/x", status="2xx")
    metrics.observe("test_duration_seconds", 0.3, route="/api/x")

    response = ops_client.get("/api/metrics")

    assert response.status_code == 200
    body = response.text
    assert 'test_counter_total{route="/api/x",status="2xx"} 1' in body
    assert "# TYPE test_counter_total counter" in body
    assert "# TYPE test_duration_seconds histogram" in body
    assert 'test_duration_seconds_bucket{le="0.5",route="/api/x"} 1' in body
    assert "test_duration_seconds_count" in body
    metrics.reset()


def test_metrics_endpoint_escapes_quotes_in_label_values(monkeypatch, ops_client):
    """标签值里的引号/反斜杠必须转义，否则整份 exposition 会解析失败。"""
    from app.services.metrics import metrics

    monkeypatch.setattr(settings, "metrics_enabled", True)
    metrics.reset()
    metrics.increment("test_total", route='we"ird\\path')

    body = ops_client.get("/api/metrics").text

    assert 'route="we\\"ird\\\\path"' in body
    metrics.reset()


def test_metrics_output_has_no_labels_suffix_for_empty_labels(monkeypatch, ops_client):
    """无标签时输出 `name 1` 而不是 `name{} 1`：两种都合法，但后者会让 grep 变麻烦。"""
    from app.services.metrics import metrics

    monkeypatch.setattr(settings, "metrics_enabled", True)
    metrics.reset()
    metrics.increment("bare_total")

    body = ops_client.get("/api/metrics").text

    assert "bare_total 1" in body
    assert "bare_total{}" not in body
    metrics.reset()


def test_ops_routes_are_not_in_openapi():
    """/health 与 /api/metrics 不该出现在 API 文档里：它们是运维端点，不是业务契约。"""
    from app.server import create_app

    spec = create_app().openapi()

    assert "/health" not in spec["paths"]
    assert "/health/ready" not in spec["paths"]
    assert "/api/metrics" not in spec["paths"]


def test_default_buckets_cover_the_real_stream_budget():
    """桶上界必须盖住单轮对话的真实总时长预算，否则长流全落 `+Inf`、p95 不可读。

    这条曾经真的坏过：桶上界是 60 秒，而注释里写着「`TOTAL_STREAM_BUDGET_SECONDS`
    是 120」——真实值是 600。两处都错，于是 60~600 秒的样本一个都进不了任何桶，
    首字延迟的分位数在长流场景下静默失真。
    """
    from app.services.chat_service import TOTAL_STREAM_BUDGET_SECONDS
    from app.services.metrics import DEFAULT_BUCKETS

    assert list(DEFAULT_BUCKETS) == sorted(DEFAULT_BUCKETS), "桶必须递增"
    assert DEFAULT_BUCKETS[-1] >= TOTAL_STREAM_BUDGET_SECONDS, (
        f"桶上界 {DEFAULT_BUCKETS[-1]} 小于单轮预算 {TOTAL_STREAM_BUDGET_SECONDS}：长流会落进 +Inf"
    )
