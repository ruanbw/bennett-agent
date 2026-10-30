"""被测目标：`prompt_lab`（离线、真实提示词）与 `live`（真实实例）。"""

from quality.targets.base import Target, TargetError
from quality.targets.live import LiveTarget
from quality.targets.prompt_lab import PromptLabTarget

__all__ = ["LiveTarget", "PromptLabTarget", "Target", "TargetError"]
