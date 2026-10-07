import httpx
from app.config import settings
from app.schemas.chat import ModelItem, ModelListResponse


async def fetch_available_models() -> ModelListResponse:
    """尝试从上游网关动态获取可用模型列表；若失败则兜底使用 settings.model_name。"""
    default_model_id = settings.model_name

    try:
        url = f"{settings.litellm_api_base.rstrip('/')}/v1/models"
        headers = {}
        if settings.litellm_api_key:
            headers["Authorization"] = f"Bearer {settings.litellm_api_key}"

        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                if data:
                    models = []
                    found_default = False
                    for item in data:
                        mid = item.get("id")
                        if not mid:
                            continue
                        display_name = item.get("display_name") or mid
                        owned_by = item.get("owned_by") or (mid.split("/")[0] if "/" in mid else "Custom")
                        is_def = (mid == default_model_id)
                        if is_def:
                            found_default = True
                        # 解析模型支持的思考等级
                        raw_levels = item.get("supported_reasoning_levels", [])
                        supported_levels = []
                        if isinstance(raw_levels, list):
                            for lvl in raw_levels:
                                if isinstance(lvl, dict) and "effort" in lvl:
                                    supported_levels.append(str(lvl["effort"]))
                                elif isinstance(lvl, str):
                                    supported_levels.append(lvl)

                        models.append(
                            ModelItem(
                                id=mid,
                                name=display_name,
                                provider=owned_by,
                                description=item.get("magpie_label") or f"{owned_by} {display_name}",
                                reasoning=bool(item.get("reasoning", False)),
                                supported_reasoning_levels=supported_levels,
                                context_window=item.get("context_window"),
                                max_output_tokens=item.get("max_output_tokens"),
                                is_default=is_def,
                            )
                        )
                    if not found_default and models:
                        models[0].is_default = True
                        default_model_id = models[0].id
                    return ModelListResponse(models=models, default_model=default_model_id)
    except Exception:
        pass

    # 兜底逻辑
    parts = default_model_id.split("/", 1)
    provider, name = (parts[0], parts[1]) if len(parts) == 2 else ("Custom", default_model_id)
    fallback_model = ModelItem(
        id=default_model_id,
        name=name,
        provider=provider,
        description=f"{provider} {name}",
        reasoning=True,
        supported_reasoning_levels=["low", "medium", "high"],
        is_default=True,
    )
    return ModelListResponse(models=[fallback_model], default_model=default_model_id)

