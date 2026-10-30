"""quality：评测 / 调优 / 安全管控三层能力，跑在 bennett-agent 自己身上。

设计边界（详见 `quality/INTERFACES.md` 与 `docs/quality-platform.md`）：
- 只读 Trace 判分，不读被测系统内部状态；
- 安全项一律走 gate，不与质量分加权求和；
- 每次运行记录 suite/target 指纹与审计行，报告可复现。
"""

from quality.budget import Budget
from quality.models import RunReport, SampleVerdict, Task, Trace

__all__ = ["Budget", "RunReport", "SampleVerdict", "Task", "Trace"]
