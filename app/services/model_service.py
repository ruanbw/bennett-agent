from typing import List, Optional
import httpx
from app.config import settings
from app.schemas.chat import ModelItem, ModelListResponse


# 静态兜底与展示优化映射表
PROVIDER_MAP = {
    "antigravity": "Google",
    "opencode-go": "DeepSeek / OpenCode",
    "discovery-api-intern-ai-org": "Qwen / 阿里",
    "amd": "AMD AI",
    "stepfun": "阶跃星辰 (StepFun)",
}


async def fetch_available_models() -> ModelListResponse:
    """从 LiteLLM / Upstream 获取真实可用的模型列表，并规范化返回给前端。"""
    default_model = settings.model_name
    models: List[ModelItem] = []

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            headers = {}
            if settings.litellm_api_key:
                headers["Authorization"] = f"Bearer {settings.litellm_api_key}"
            
            resp = await client.get(f"{settings.litellm_api_base.rstrip('/')}/v1/models", headers=headers)
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                for item in data:
                    model_id = item.get("id")
                    if not model_id:
                        continue
                    
                    owned_by = item.get("owned_by", "ai")
                    provider_name = PROVIDER_MAP.get(owned_by, owned_by)
                    display_name = item.get("display_name") or model_id.split("/")[-1]
                    
                    models.append(
                        ModelItem(
                            id=model_id,
                            name=display_name,
                            provider=provider_name,
                            description=item.get("magpie_label") or f"{provider_name} {display_name}",
                            reasoning=bool(item.get("reasoning", False)),
                            context_window=item.get("context_window") or item.get("context_length"),
                            max_output_tokens=item.get("max_output_tokens"),
                            is_default=(model_id == default_model),
                        )
                    )
    except Exception as e:
        print(f"[ModelService] 获取上游模型列表失败，使用本地兜底: {e}")

    # 如果无法拉取或为空，提供默认配置
    if not models:
        models = [
            ModelItem(
                id=settings.model_name,
                name=settings.model_name.split("/")[-1],
                provider="Default Provider",
                description="默认模型",
                reasoning=True,
                is_default=True,
            )
        ]

    # 确保当前 settings.model_name 在列表首位或标记为 is_default
    has_default = any(m.is_default for m in models)
    if not has_default and models:
        models[0].is_default = True
        default_model = models[0].id

    return ModelListResponse(models=models, default_model=default_model)
