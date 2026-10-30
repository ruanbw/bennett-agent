"""被测目标协议：评测层只通过它触发一次执行，只从 `Trace` 读结果。

为什么不做成「直接 import app 并跑起来」：

- 生产 agent 需要数据库、Redis、上游模型额度，任何一样缺失评测就跑不动；
- 评测一旦依赖生产装配，被测系统的内部改动会悄悄改变判分口径，报告逐年不可比。

所以 Targets 分成两层：
- `PromptLabTarget`：真实系统提示词 + 真实工具契约 + 可注入模型/工具行为，离线可复现，
  用来做日常回归与调优闭环；
- `LiveTarget`：打真实 HTTP 端点，用来在发布前做一次端到端确认。
两者产出同一种 `Trace`，因此判分器与报告只写一份。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from quality.models import Task, Trace


@runtime_checkable
class Target(Protocol):
    """被测系统。实现必须是可重复的：同一 (task, sample) 至少配置相同时结果稳定。"""

    name: str

    async def run(self, task: Task, sample: int) -> Trace:
        """执行一次任务并返回观察。失败也要返回 Trace（error 字段说明原因），不要抛异常。"""
        ...

    def config_fingerprint(self) -> str:
        """当前配置（模型、提示词、工具集）的指纹，写进报告用于复现。"""
        ...


class TargetError(RuntimeError):
    """装配阶段失败（不是单次执行失败）。执行期失败写进 Trace.error。"""
