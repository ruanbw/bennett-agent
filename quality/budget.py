"""预算闸：评测最贵的失败不是「跑错」，是「跑不完还在跑」。

企业里一次全量回归可能烧掉几百美元上游额度。这里的口径：

- 预算分三项：token、美元、墙钟时长。任一耗尽即停止派发新样本；
- 已派发的样本不打断（打断会让报告缺样本，比超一点预算更难解释）；
- 耗尽不是异常：`exhausted` 说明原因，报告里标记 partial，门禁据此拒绝放行。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Budget:
    """可选的成本上限。全部为 None 表示不设限（只适合小样本本地跑）。"""

    max_tokens: int | None = None
    max_usd: float | None = None
    max_seconds: float | None = None

    tokens_used: int = 0
    usd_used: float = 0.0
    started_at: float = field(default_factory=time.monotonic)
    stopped_reason: str | None = None

    def charge(self, *, tokens: int = 0, usd: float = 0.0) -> None:
        self.tokens_used += max(0, tokens)
        self.usd_used += max(0.0, usd)

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def exhausted(self) -> bool:
        return self.stopped_reason is not None

    def check(self) -> str | None:
        """返回耗尽原因（并记住它），未耗尽返回 None。"""
        if self.stopped_reason is not None:
            return self.stopped_reason
        reason: str | None = None
        if self.max_tokens is not None and self.tokens_used >= self.max_tokens:
            reason = f"token 预算耗尽（{self.tokens_used}/{self.max_tokens}）"
        elif self.max_usd is not None and self.usd_used >= self.max_usd:
            reason = f"美元预算耗尽（${self.usd_used:.4f}/${self.max_usd:.4f}）"
        elif self.max_seconds is not None and self.elapsed_seconds >= self.max_seconds:
            reason = f"时长预算耗尽（{self.elapsed_seconds:.1f}s/{self.max_seconds:.1f}s）"
        if reason:
            self.stopped_reason = reason
        return reason

    def to_dict(self) -> dict[str, float | int | str | None]:
        return {
            "max_tokens": self.max_tokens,
            "max_usd": self.max_usd,
            "max_seconds": self.max_seconds,
            "tokens_used": self.tokens_used,
            "usd_used": round(self.usd_used, 6),
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "stopped_reason": self.stopped_reason,
        }
