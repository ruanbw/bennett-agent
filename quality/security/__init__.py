"""安全扫描：内置探针（OWASP 映射）+ 扫描聚合。

调用方通常只需要 `build_security_suite()` 造用例、`scan()` 拿 `SecurityReport`、
`scan_failed()` 决定退出码。探针与聚合分开是为了让两边都能单独测：
探针是纯数据，聚合是纯函数（只依赖一条执行器调用）。
"""

from __future__ import annotations

from quality.security.probes import (
    CANARY,
    MARKER,
    OWASP_LABELS,
    SECURITY_SUITE_ID,
    build_security_suite,
    owasp_of,
    security_probe_tasks,
)
from quality.security.scan import owasp_label, scan, scan_failed

__all__ = [
    "CANARY",
    "MARKER",
    "OWASP_LABELS",
    "SECURITY_SUITE_ID",
    "build_security_suite",
    "owasp_label",
    "owasp_of",
    "scan",
    "scan_failed",
    "security_probe_tasks",
]
