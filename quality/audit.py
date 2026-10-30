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
        #: 本进程写过的行。扫描结束后要取回「有几条尝试没取得可判证据」（`scan.errored`），
        #: 而 `SecurityReport` 是冻结契约、没地方放这个计数；回读正在追加的 jsonl 更脏。
        #: 只活在内存里，不参与持久化语义。
        self.entries: list[AuditEntry] = []

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
        self.entries.append(entry)
        return entry
