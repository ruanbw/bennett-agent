"""append-only 审计日志。

企业采购会问三个问题：谁跑的、跑的是什么版本、结论是什么。JSONL 追加写，
一行一条，不覆盖、不改写——覆盖式日志在争议时等于没有日志。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from quality.models import AuditEntry


class AuditLog:
    """写入 `<run_dir>/audit.jsonl`；也可以指向集中审计目录。"""

    def __init__(self, path: str | Path, *, actor: str, tenant: str) -> None:
        self.path = Path(path)
        self.actor = actor
        self.tenant = tenant

    def append(self, action: str, subject: str, *, outcome: str = "ok", **detail: object) -> AuditEntry:
        entry = AuditEntry(
            actor=self.actor,
            tenant=self.tenant,
            action=action,
            subject=subject,
            outcome=outcome,
            detail=dict(detail),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # O_APPEND + 单行写入：并发进程下不会互相覆盖前缀。
        line = json.dumps(entry.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        return entry
