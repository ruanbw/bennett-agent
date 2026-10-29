"""运维端点：健康检查与指标导出；指标端点免登录但默认关闭（`METRICS_ENABLED=false`）。
`/health` 是存活（不碰 DB/上游，失败就该重启），`/health/ready` 是就绪，只把
`READINESS_REQUIRED_DEPENDENCIES`（默认 database）的失败判 503，其余降级为 `degraded`。
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import text

from app.config import settings
from app.db import audit_provider_key_decryption, engine
from app.services.metrics import metrics
from app.services.redis_client import redis_health_detail
from app.services.storage import StorageNotConfigured, get_object_store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ops"])

#: readiness 探测数据库的超时（秒）：太长会被探针判超时，失败原因就变成「探针慢」而不是「数据库慢」。
READY_DB_TIMEOUT_SECONDS = 2.0


@router.get(
    "/health",
    summary="存活探针（不触碰数据库与上游）",
    include_in_schema=False,
)
async def health() -> dict[str, object]:
    """进程是否还能处理请求（不查任何依赖）。
    真去 ping 数据库会让 liveness 在数据库抖动时触发重启，而重启解决不了它、只会丢掉在途请求；
    就绪判定看 `/health/ready`。
    """
    from app.version import APP_VERSION

    return {"status": "ok", "version": APP_VERSION}


def required_dependencies() -> frozenset[str]:
    """本次部署把哪些依赖视为必需。
    未知名字忽略并告警：写错一个词不该让启动失败，也不能静默当成「不用检查」——那会让该摘流的实例继续接流量。
    """
    known = {"database", "redis", "storage", "provider_keys"}
    raw = (settings.readiness_required_dependencies or "").split(",")
    items = {item.strip().lower() for item in raw if item.strip()}
    unknown = items - known
    if unknown:
        logger.warning("READINESS_REQUIRED_DEPENDENCIES 含未知项，已忽略: %s", ", ".join(sorted(unknown)))
    return frozenset(items & known)


@router.get(
    "/health/ready",
    summary="就绪探针（数据库 / Redis / 对象存储）",
    include_in_schema=False,
)
async def health_ready(response: Response) -> dict[str, object]:
    """逐项探测依赖，按配置决定是否把实例摘出负载均衡。
    不检查上游模型：模型挂了不该摘掉整个实例（那样连历史会话都看不了）；必需依赖失败返回 503，
    非必需失败只降级为 `degraded`（仍 200），理由见模块 docstring。
    """
    required = required_dependencies()

    # 用 `wait_for`：`asyncio.timeout` 是 3.11 才有的，本项目最低支持 3.10。
    async def ping_db() -> None:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    try:
        await asyncio.wait_for(ping_db(), timeout=READY_DB_TIMEOUT_SECONDS)
        database: dict[str, object] = {"status": "ok"}
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning("readiness 检查超时（数据库 %.1fs 内无响应）", READY_DB_TIMEOUT_SECONDS)
        database = {"status": "unavailable", "reason": "timeout"}
    except Exception:  # noqa: BLE001 - 任何 DB 故障都归成「不可用」
        logger.warning("readiness 检查失败：数据库不可用", exc_info=True)
        database = {"status": "unavailable", "reason": "unavailable"}

    checks: dict[str, dict[str, object]] = {
        "database": database,
        "redis": await redis_health_detail(timeout=READY_DB_TIMEOUT_SECONDS),
        "storage": await _storage_health(),
        "provider_keys": await _provider_keys_health(),
    }
    failed_required = [name for name in required if str(checks[name].get("status")) != "ok"]
    if failed_required:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        overall = "not_ready"
    elif any(str(check.get("status")) not in {"ok", "disabled"} for check in checks.values()):
        overall = "degraded"
    else:
        overall = "ready"

    return {"status": overall, "required": sorted(required), **checks}


async def _provider_keys_health() -> dict[str, object]:
    """提供商的 API Key 是否都还能解密：换过 `AUTH_SECRET` 后只表现为「上游 401 + 后台显示未设置」。
    不参与 `required` 判定：它只影响对话，算成不健康会让「只有模型调用失败」的实例被整体摘流。
    """
    try:
        broken = await audit_provider_key_decryption()
    except Exception:  # noqa: BLE001 - 探针不该把异常抛给编排器
        logger.warning("readiness 检查失败：API Key 解密自检异常", exc_info=True)
        return {"status": "unknown", "reason": "error"}
    if broken:
        return {"status": "degraded", "reason": "undecryptable", "providers": broken}
    return {"status": "ok"}


async def _storage_health() -> dict[str, object]:
    """对象存储探测（服务沙箱产物导出）。未开沙箱时显式报告 `disabled`，而不是假装 ok。"""
    if not settings.sandbox_enabled:
        return {"status": "disabled"}
    try:
        ok, reason = await get_object_store().health()
    except StorageNotConfigured:
        return {"status": "unavailable", "reason": "not_configured"}
    except Exception:  # noqa: BLE001 - 探针不该把异常抛给编排器
        logger.warning("readiness 检查失败：对象存储不可用", exc_info=True)
        return {"status": "unavailable", "reason": "error"}
    return {"status": "ok"} if ok else {"status": "unavailable", "reason": reason}


@router.get(
    "/api/metrics",
    summary="进程内指标（Prometheus 文本格式）",
    response_class=PlainTextResponse,
    include_in_schema=False,
)
async def metrics_endpoint() -> Response:
    """导出计数器与延迟直方图；关闭时（`METRICS_ENABLED=false`）返回 404 而不是 403。
    免登录端点默认关闭：多开一个就多一分侦察面；404 比「存在但你不能看」泄露得更少。
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
