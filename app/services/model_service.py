from app.config import settings
from app.schemas.chat import ModelItem, ModelListResponse


async def fetch_available_models() -> ModelListResponse:
    """根据配置直接返回当前配置的模型信息，配置什么就展示什么。"""
    model_id = settings.model_name
    parts = model_id.split("/", 1)
    if len(parts) == 2:
        provider, name = parts[0], parts[1]
    else:
        provider, name = "Custom", model_id

    models = [
        ModelItem(
            id=model_id,
            name=name,
            provider=provider,
            description=f"{provider} {name}",
            reasoning=True,
            is_default=True,
        )
    ]

    return ModelListResponse(models=models, default_model=model_id)

