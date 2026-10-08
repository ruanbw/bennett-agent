"""模型列表服务：从上游网关拉取可用模型，失败时兜底。"""

import logging
import time

from app.config import settings
from app.schemas.chat import ModelItem, ModelListResponse
from app.services.http_client import get_http_client

logger = logging.getLogger(__name__)

#: 拉取模型列表是页面加载路径上的一步，3 秒没结果就该走兜底。
MODELS_TIMEOUT_SECONDS = 3.0

#: 缓存有效期。网关的模型列表变化很慢，而每次页面加载都打上游既慢又浪费。
MODELS_CACHE_TTL_SECONDS = 300.0

_cache: tuple[float, ModelListResponse] | None = None


def _build_url() -> str:
    base = settings.litellm_api_base.rstrip("/")
    return f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"


def _parse_reasoning_levels(raw: object) -> list[str]:
    """解析模型支持的思考等级。

    网关的返回有两种形状：`[{"effort": "high"}]` 或 `["high"]`。两种都接受，
    其它形状忽略——伪造等级会让用户选了之后请求失败。
    """
    if not isinstance(raw, list):
        return []

    levels: list[str] = []
    for item in raw:
        if isinstance(item, dict) and "effort" in item:
            levels.append(str(item["effort"]))
        elif isinstance(item, str):
            levels.append(item)
    return levels


def _parse_models(
    payload: object,
    default_model_id: str,
    description_field: str,
) -> ModelListResponse | None:
    """把上游响应转成 `ModelListResponse`；无可用模型时返回 None。

    `description_field` 由配置传入：上游网关可以提供一个比 'provider name' 更友好
    的展示标签，但字段名属于网关细节，不该硬编码在这里。
    """
    if not isinstance(payload, dict):
        return None

    data = payload.get("data")
    if not isinstance(data, list):
        return None

    models: list[ModelItem] = []
    found_default = False

    for item in data:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not model_id:
            continue

        display_name = item.get("display_name") or model_id
        owned_by = item.get("owned_by") or (model_id.split("/")[0] if "/" in model_id else "Custom")
        is_default = model_id == default_model_id
        found_default = found_default or is_default

        models.append(
            ModelItem(
                id=model_id,
                name=display_name,
                provider=owned_by,
                description=item.get(description_field) or f"{owned_by} {display_name}",
                reasoning=bool(item.get("reasoning", False)),
                supported_reasoning_levels=_parse_reasoning_levels(
                    item.get("supported_reasoning_levels", [])
                ),
                context_window=item.get("context_window"),
                max_output_tokens=item.get("max_output_tokens"),
                is_default=is_default,
            )
        )

    if not models:
        return None

    if not found_default:
        # 没有匹配到配置的默认模型时，把第一个标为默认，避免前端没有可选项
        models[0].is_default = True
        default_model_id = models[0].id

    return ModelListResponse(models=models, default_model=default_model_id)


def _fallback(default_model_id: str) -> ModelListResponse:
    """兜底：只报配置里的那个模型。

    刻意不伪造思考能力与支持等级——`reasoning=False` + 空等级会让前端不展示
    思考等级选择器，从而避免「用户选了却请求失败」。
    """
    provider, _, name = default_model_id.partition("/")
    if not name:
        provider, name = "Custom", default_model_id

    return ModelListResponse(
        models=[
            ModelItem(
                id=default_model_id,
                name=name,
                provider=provider,
                description=f"{provider} {name}",
                reasoning=False,
                supported_reasoning_levels=[],
                is_default=True,
            )
        ],
        default_model=default_model_id,
    )


def reset_models_cache() -> None:
    """清空缓存。供测试与「改了配置想立刻生效」的场景使用。"""
    global _cache
    _cache = None


async def fetch_available_models(*, use_cache: bool = True) -> ModelListResponse:
    """获取可用模型列表；上游不可达时退回兜底配置。"""
    global _cache

    default_model_id = settings.model_name

    if use_cache and _cache is not None:
        cached_at, cached = _cache
        if time.monotonic() - cached_at < MODELS_CACHE_TTL_SECONDS:
            return cached

    result = await _fetch_from_gateway(default_model_id)
    _cache = (time.monotonic(), result)
    return result


async def _fetch_from_gateway(default_model_id: str) -> ModelListResponse:
    headers = {}
    if settings.litellm_api_key:
        headers["Authorization"] = f"Bearer {settings.litellm_api_key}"

    try:
        response = await get_http_client().get(
            _build_url(),
            headers=headers,
            timeout=MODELS_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning("从上游网关拉取模型列表失败，将使用默认兜底配置: %s", exc)
        return _fallback(default_model_id)

    if response.status_code != 200:
        logger.warning("上游 /v1/models 响应非 200: %s", response.status_code)
        return _fallback(default_model_id)

    try:
        payload = response.json()
    except ValueError:
        logger.warning("上游 /v1/models 返回的不是 JSON")
        return _fallback(default_model_id)

    parsed = _parse_models(payload, default_model_id, settings.model_description_field)
    if parsed is None:
        logger.warning("上游 /v1/models 未返回可用模型，将使用默认兜底配置")
        return _fallback(default_model_id)

    return parsed
