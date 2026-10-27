"""旧版环境变量网关：直接读 `LITELLM_API_BASE` 的 `/v1/models`。

库里一条提供商都没有时（旧部署、测试、尚未引导）才走这条路径。它只为了升级不中断，
不是第二套配置入口：首次启动会用环境变量**引导**出一条「默认网关」提供商，之后由
后台接管（见 `registry.seed_default_provider`）。

这一条路径的缓存保持原样（数据库路径不缓存，理由见包 docstring）：网关的模型列表
变化很慢，而每次页面加载都打上游既慢又浪费；失败还要单独负缓存，避免慢失败的兜底
顶掉刚拿到的成功列表。
"""

import asyncio
import logging
import re
import time
from urllib.parse import urlsplit

from app.config import settings
from app.schemas.chat import ModelItem, ModelListResponse
from app.services.http_client import get_http_client
from app.services.model_service.upstream import (
    parse_upstream_models,
)

logger = logging.getLogger(__name__)

#: 路径里形如 `v1` / `v3` / `v1beta` 的段。见 `_build_models_url`。
_VERSION_SEGMENT = re.compile(r"^v\d+")

#: 拉取模型列表是页面加载路径上的一步，3 秒没结果就该走兜底。
MODELS_TIMEOUT_SECONDS = 3.0

#: 缓存有效期。网关的模型列表变化很慢，而每次页面加载都打上游既慢又浪费。
MODELS_CACHE_TTL_SECONDS = 300.0

#: 兜底结果缓存有效期（负缓存）。避免上游瞬时抖动造成过长时间锁定，同时防止频繁超时。
FALLBACK_CACHE_TTL_SECONDS = 10.0

#: 成功结果的缓存：(写入时刻, 响应)。只有成功结果会写进这一格。
_success_cache: tuple[float, ModelListResponse] | None = None
#: 负缓存：最近一次「上游不可用」产生的兜底响应。刻意与成功缓存分开存——失败永远
#: 顶不掉已有的成功结果（共用一格时，慢失败的兜底会盖掉刚拿到的成功列表）。
_fallback_cache: tuple[float, ModelListResponse] | None = None

#: single-flight 锁，以及它绑定的 loop。锁按事件循环惰性重建：asyncio.Lock 会绑定
#: 首次使用它的 loop，模块级常量锁在「每个用例一个 loop」的测试环境里会 RuntimeError。
_lock: asyncio.Lock | None = None
_lock_loop: asyncio.AbstractEventLoop | None = None


def _build_models_url(base_url: str) -> str:
    """根据提供商的 base_url 拼出模型列表地址。

    兼容两大类写法：

    - **已带版本段**：`http://host:4000/v1`、`https://host/api/paas/v4`（智谱）、
      `https://host/api/v3`（火山方舟）、`https://host/v1beta/openai`（Gemini 兼容层）。
      路径里任何一段形如 `v<数字>` 就认为版本已经在地址里，直接拼 `/models`；
    - **裸主机**：`http://host:4000`（OpenAI SDK 的 base_url 习惯），补上 `/v1/models`。

    重复拼 `/v1` 会让提供商返回 404，而 404 又很容易被误读成「提供商不可用」。
    """
    base = base_url.strip().rstrip("/")
    path = urlsplit(base).path
    if any(_VERSION_SEGMENT.match(segment) for segment in path.split("/") if segment):
        return f"{base}/models"
    return f"{base}/v1/models"


def _build_url() -> str:
    return _build_models_url(settings.litellm_api_base)


def _parse_models(
    payload: object,
    default_model_id: str,
    description_field: str,
    *,
    mark_default: bool = True,
) -> ModelListResponse | None:
    """把上游响应转成 `ModelListResponse`；无可用模型时返回 None。

    信封与字段别名解析统一走 `upstream.parse_upstream_models`（与同步路径共用一份，
    避免两套解析各自漂移）；这里只保留网关路径自己的两件事：

    - `mark_default=False` 用于管理后台的同步：默认模型由数据库行决定，这里不该把
      上游列表的第一项强行标成默认（也不该为「配置的默认模型」打一条误导性日志）。
    - 没匹配到配置的默认模型时回退为列表第一项，让前端始终有一个可选项。
    """
    entries = parse_upstream_models(payload, description_field)
    if not entries:
        return None

    models: list[ModelItem] = []
    found_default = False

    for entry in entries:
        is_default = entry.model_id == default_model_id
        found_default = found_default or is_default
        models.append(
            ModelItem(
                id=entry.model_id,
                name=entry.display_name or entry.model_id,
                provider=entry.owned_by,
                description=entry.description,
                reasoning=entry.reasoning,
                supported_reasoning_levels=entry.supported_reasoning_levels,
                context_window=entry.context_window,
                max_output_tokens=entry.max_output_tokens,
                is_default=is_default,
            )
        )

    if not found_default and mark_default:
        # 没有匹配到配置的默认模型时，把第一个标为默认，避免前端没有可选项
        logger.info(
            "配置的默认模型 %s 不在网关返回的列表中，回退为列表首个模型 %s",
            default_model_id,
            models[0].id,
        )
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
    global _success_cache, _fallback_cache
    _success_cache = None
    _fallback_cache = None


def _get_lock() -> asyncio.Lock:
    """取当前事件循环的 single-flight 锁；loop 变了就换一把新的。"""
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock = asyncio.Lock()
        _lock_loop = loop
    return _lock


def _read_fresh_cache() -> tuple[ModelListResponse, bool] | None:
    """命中未过期的缓存时返回 (响应, 是否兜底)。"""
    now = time.monotonic()
    if _success_cache is not None and now - _success_cache[0] < MODELS_CACHE_TTL_SECONDS:
        return _success_cache[1], False
    if _fallback_cache is not None and now - _fallback_cache[0] < FALLBACK_CACHE_TTL_SECONDS:
        return _fallback_cache[1], True
    return None


async def _load_models(*, use_cache: bool) -> tuple[ModelListResponse, bool]:
    """取模型列表（含 single-flight 与缓存写入）。"""
    global _success_cache, _fallback_cache

    if use_cache:
        cached = _read_fresh_cache()
        if cached is not None:
            return cached

    async with _get_lock():
        # 双检：等锁期间先到的请求可能已经把结果写进缓存了，此时不该再打一次上游
        if use_cache:
            cached = _read_fresh_cache()
            if cached is not None:
                return cached

        result, is_fallback = await _fetch_from_gateway(settings.model_name)
        if is_fallback:
            # 失败只进负缓存，绝不覆盖成功缓存：否则上游刚恢复又抖了一下，
            # 用户会在一分钟内看到兜底模型
            _fallback_cache = (time.monotonic(), result)
        else:
            _success_cache = (time.monotonic(), result)
            _fallback_cache = None
        return result, is_fallback


async def _fetch_from_gateway(default_model_id: str) -> tuple[ModelListResponse, bool]:
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
        return _fallback(default_model_id), True

    if response.status_code != 200:
        logger.warning("上游 /v1/models 响应非 200: %s", response.status_code)
        return _fallback(default_model_id), True

    try:
        payload = response.json()
    except ValueError:
        logger.warning("上游 /v1/models 返回的不是 JSON")
        return _fallback(default_model_id), True

    try:
        parsed = _parse_models(payload, default_model_id, settings.model_description_field)
    except Exception as exc:
        logger.warning("解析上游 /v1/models 响应失败，将使用默认兜底配置: %s", exc)
        return _fallback(default_model_id), True

    if parsed is None:
        logger.warning("上游 /v1/models 未返回可用模型，将使用默认兜底配置")
        return _fallback(default_model_id), True

    return parsed, False
