"""一次模型调用的解析结果：上游是谁、用哪套凭据、路由键是什么。
放这里是为了避开 `model_service` ↔ `agent_factory` 的循环导入；`key` 是对外路由键 `{slug}/{model_id}`。
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelTarget:
    """已解析的模型调用目标。"""

    key: str
    upstream_model: str
    provider_name: str
    base_url: str
    api_key: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    reasoning: bool | None = None
    fallback: "ModelTarget | None" = None
