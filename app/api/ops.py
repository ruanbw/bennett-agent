"""运维端点：健康检查与进程内指标导出。

## 健康检查要分两个层次

- `GET /health`：**存活**。不碰数据库、不碰上游，只证明进程还能处理请求。容器
  liveness probe 用这个——它一旦失败就该重启进程，而数据库抖动不该触发重启
  （重启解决不了数据库的问题，只会把在途请求全丢掉）。
- `GET /health/ready`：**就绪**。真的去 ping 数据库。readiness probe 用这个：
  数据库不可用时应该把流量摘走，而不是让用户撞上一连串 500。

把两者合成一个端点是最常见的错误：要么数据库抖一下就触发重启风暴，要么进程假活
着却一直 500。

## 指标端点为什么不需要登录

`/api/metrics` 暴露的是**聚合计数**（请求数、延迟分桶、错误率），不含任何用户数据、
没有 URL 与 query、没有用户 id。它需要被 Prometheus 之类的采集器直接抓，加认证会让
部署多一层凭据管理。

代价是任何人都能看到「这个服务有多少请求、多慢」——这个信息量对攻击者的价值很低，
而漏掉它会让运维在排障时瞎猜。**如果部署方认为这也算侦察面**，用
`METRICS_ENABLED=false` 关掉整个端点（默认关闭——见 `settings.metrics_enabled`
的说明）。

## 输出格式

Prometheus 的文本 exposition 格式：`# HELP` / `# TYPE` 注释加 `name{labels} value`。
不引 `prometheus_client` 的理由见 `app/services/metrics.py`。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import text

from app.config import settings
from app.db import engine
from app.services.metrics import metrics

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ops"])

#: readiness 探测数据库的超时（秒）。不能太长：探针本身有超时，卡住会被判失败，
#: 而失败原因应当明确是「数据库慢」，不是「探针超时」。
READY_DB_TIMEOUT_SECONDS = 2.0


@router.get(
    "/health",
    summary="存活探针（不触碰数据库与上游）",
    include_in_schema=False,
)
async def health() -> dict[str, object]:
    """进程是否还能处理请求。

    刻意什么都不检查：这个端点的语义是「进程活着」，不是「服务可用」。真去 ping
    数据库会让 liveness probe 在数据库抖动时触发重启——重启解决不了数据库的问题，
    只会把在途请求一起丢掉。就绪判定看 `/health/ready`。
    """
    from app.version import APP_VERSION

    return {"status": "ok", "version": APP_VERSION}


@router.get(
    "/health/ready",
    summary="就绪探针（会 ping 数据库）",
    include_in_schema=False,
)
async def health_ready(response: Response) -> dict[str, object]:
    """依赖是否就绪：这里只查数据库。

    不检查上游模型：它们本来就是「可能失败」的外部依赖，模型挂了不该让整个实例被
    摘出负载均衡——那样连历史会话都看不了。模型失败由指标与备用模型机制暴露。

    数据库失败时返回 503：让编排器把流量摘走，用户就不会撞上一连串 500。
    """

    # 用 `wait_for` 而不是 `asyncio.timeout`：后者是 Python 3.11 才有的，本项目最低
    # 支持 3.10（`chat_service` 与 `mcp_client` 里也都是这个写法）。
    async def ping() -> None:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    try:
        await asyncio.wait_for(ping(), timeout=READY_DB_TIMEOUT_SECONDS)
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning("readiness 检查超时（数据库 %.1fs 内无响应）", READY_DB_TIMEOUT_SECONDS)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "database": "timeout"}
    except Exception:  # noqa: BLE001 - 任何 DB 故障都归成「未就绪」
        logger.warning("readiness 检查失败：数据库不可用", exc_info=True)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "database": "unavailable"}

    return {"status": "ready", "database": "ok"}


@router.get(
    "/api/metrics",
    summary="进程内指标（Prometheus 文本格式）",
    response_class=PlainTextResponse,
    include_in_schema=False,
)
async def metrics_endpoint() -> Response:
    """导出计数器与延迟直方图。

    默认**关闭**（`METRICS_ENABLED=false`）：它是一个不需要登录的端点，多开一个
    就多一分侦察面，而绝大多数部署并没有采集器。关掉时返回 404 而不是 403——
    「这个端点不存在」比「存在但你不能看」泄露的信息更少。
    """
    if not settings.metrics_enabled:
        return Response(status_code=status.HTTP_404_NOT_FOUND)

    lines: list[str] = []
    for sample in metrics.snapshot():
        lines.extend(_render_sample(sample))

    body = "\n".join(lines) + "\n" if lines else ""
    return PlainTextResponse(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")


def _render_sample(sample) -> list[str]:  # noqa: ANN001 - MetricSample
    """把一条样本渲染成 Prometheus 文本格式。"""
    label_text = _format_labels(sample.labels)

    if sample.buckets is not None:
        lines = [
            f"# TYPE {sample.name} histogram",
        ]
        for bound, count in sample.buckets:
            bound_text = "+Inf" if bound == float("inf") else _format_float(bound)
            lines.append(f"{sample.name}_bucket{_format_labels({**sample.labels, 'le': bound_text})} {count}")
        lines.append(f"{sample.name}_count{label_text} {sample.count}")
        lines.append(f"{sample.name}_sum{label_text} {_format_float(sample.sum_seconds)}")
        return lines

    return [
        f"# TYPE {sample.name} counter",
        f"{sample.name}{label_text} {_format_float(sample.value or 0.0)}",
    ]


def _format_labels(labels: dict[str, str]) -> str:
    """渲染标签串；没有标签时返回空串（`name 1` 而不是 `name{} 1`）。"""
    if not labels:
        return ""
    inner = ",".join(f'{name}="{_escape_label_value(value)}"' for name, value in sorted(labels.items()))
    return "{" + inner + "}"


def _escape_label_value(value: str) -> str:
    """按 exposition 规范转义标签值里的反斜杠、引号与换行。"""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_float(value: float) -> str:
    """整数值不带小数点（`2` 而不是 `2.0`）：两种写法都合法，但读数时更干净。"""
    if value != value:  # NaN
        return "NaN"
    if value == float("inf"):
        return "+Inf"
    if value == int(value):
        return str(int(value))
    return repr(value)


__all__ = ["READY_DB_TIMEOUT_SECONDS", "router"]
