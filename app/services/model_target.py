"""一次模型调用的解析结果：上游是谁、用哪套凭据、路由键是什么。

为什么单独成模块：`agent_factory`、`chat_service`、`model_service` 都要传递它，
而 `model_service` 又依赖数据库与会话模型。把它放在这里可以让装配层不需要知道
数据库长什么样，也避免 `model_service` ↔ `agent_factory` 的循环导入。

字段含义：

- `key`：**对外路由键**，形如 `{provider.slug}/{model_id}`。前端、会话持久化、
  SSE 事件里都用它；不同提供商可以提供同名模型，所以不能直接拿 `model_id` 当键。
- `upstream_model`：真正发给提供商的名字。
- `base_url` / `api_key`：该提供商自己的端点与凭据。
- `fallback`：同一提供商内可选的备用模型（来自 `FALLBACK_MODEL_NAME`）。只有在上游
  明确列出且被启用时才存在；`agent_factory` 据此包 `FallbackModel`。
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
