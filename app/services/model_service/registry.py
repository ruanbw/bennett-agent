"""模型解析与对外清单：一轮对话的模型目标、可用模型列表、默认模型与窗口上限。

有任一提供商行或 registry 标记后数据库即唯一权威：禁用立即生效，故不做任何缓存。
写库与上游同步在 `provider_sync`，行级查询在 `provider_repo`，这里只负责读快照。
"""

import logging
from dataclasses import dataclass, replace

from app.config import settings
from app.db import ModelProvider, ProviderModel, async_session_maker
from app.schemas.chat import ModelItem, ModelListResponse
from app.services.fallback_config import configured_fallback_name, warn_fallback_unavailable
from app.services.model_service import gateway, outbound_guard
from app.services.model_service.provider_repo import enabled_model_rows, registry_configured
from app.services.model_target import ModelTarget

logger = logging.getLogger(__name__)


def make_model_key(provider_slug_value: str, model_id: str) -> str:
    """对外路由键：`{provider.slug}/{model_id}`。"""
    return f"{provider_slug_value}/{model_id}"


async def _build_target(model: ProviderModel, provider: ModelProvider) -> ModelTarget:
    """组装一次调用目标并在出站前校验地址；这里是唯一构造带 `base_url` 的 ModelTarget 处。

    地址非法抛 `ProviderFetchError`：返回 None 会变成「模型已停用」的误导提示。
    不缓存解析结果，否则 DNS rebinding 的窗口会从单次请求拉到整个进程生命周期。
    """
    await outbound_guard.reject_private_target(provider.base_url)
    return ModelTarget(
        key=make_model_key(provider.slug, model.model_id),
        upstream_model=model.model_id,
        provider_name=provider.name,
        base_url=provider.base_url,
        api_key=provider.api_key,
        context_window=model.context_window,
        max_output_tokens=model.max_output_tokens,
        reasoning=model.reasoning,
    )


def _configured_fallback_name(primary_name: str) -> str | None:
    """备用模型名（口径见 `app/services/fallback_config.py`）；这里只管配没配。

    配了却因未启用用不上时，调用方必须 `warn_fallback_unavailable`，否则运维以为自己有兜底。
    """
    return configured_fallback_name(primary_name)


@dataclass(frozen=True, slots=True)
class ChatModelSnapshot:
    """一轮对话的模型解析结果：路由键、调用目标、窗口/输出上限、白名单。

    `context_window` 与 `fallback_*` 分开存：备用模型接手时用量事件必须报真正回答的模型窗口。
    `key` 为空串 = 库已接管但无启用模型，调用方必须拒绝；`reasoning_levels` 为空 = 无法校验，不用于拒绝。
    """

    key: str
    target: ModelTarget | None
    context_window: int | None
    max_output_tokens: int | None
    fallback_key: str | None
    fallback_context_window: int | None
    known_ids: frozenset[str] | None
    registry_managed: bool
    reasoning_levels: frozenset[str] = frozenset()

    def context_window_for(self, answered_by: str) -> int | None:
        """本轮实际回答的模型对应的上下文窗口。"""
        if self.fallback_key is not None and answered_by == self.fallback_key:
            return self.fallback_context_window
        return self.context_window


async def provider_config_exists() -> bool:
    """True 表示管理后台是权威（即使此刻零个提供商）；False 才退回旧版环境变量网关。"""
    async with async_session_maker() as session:
        return await registry_configured(session)


async def resolve_chat_model(requested_key: str | None) -> ChatModelSnapshot:
    """解析本轮对话的模型：白名单 + 目标 + 窗口/输出上限，一次读库。

    `requested_key` 为 None 取后台默认；库路径下未知或已停用的模型返回 `target=None`，调用方必须拒绝而不是退回环境变量。
    备用模型只在同一提供商内找：跨提供商的参数兼容性无法保证（如思考等级词表不同）。
    """
    async with async_session_maker() as session:
        if not await registry_configured(session):
            rows: list[tuple[ProviderModel, ModelProvider]] = []
            registry_managed = False
        else:
            rows = await enabled_model_rows(session)
            registry_managed = True

    if not registry_managed:
        return await _resolve_from_gateway(requested_key)

    by_key = {make_model_key(provider.slug, model.model_id): (model, provider) for model, provider in rows}
    key = requested_key or _default_key_of(rows) or ""
    match = by_key.get(key)
    if match is None:
        return ChatModelSnapshot(
            key=key,
            target=None,
            context_window=None,
            max_output_tokens=None,
            fallback_key=None,
            fallback_context_window=None,
            known_ids=frozenset(by_key),
            registry_managed=True,
        )

    model, provider = match
    target = await _build_target(model, provider)
    fallback_key: str | None = None
    fallback_context_window: int | None = None

    fallback_name = _configured_fallback_name(target.upstream_model)
    if fallback_name is not None:
        matched = False
        for candidate_model, candidate_provider in rows:
            if candidate_provider.id == provider.id and candidate_model.model_id == fallback_name:
                fallback = await _build_target(candidate_model, candidate_provider)
                target = replace(target, fallback=fallback)
                fallback_key = fallback.key
                fallback_context_window = fallback.context_window
                matched = True
                break
        if not matched:
            warn_fallback_unavailable(
                target.upstream_model,
                fallback_name,
                "同一提供商内没有这个已启用的模型（跨提供商的参数兼容性无法保证，因此不做跨提供商兜底）",
            )

    return ChatModelSnapshot(
        key=key,
        target=target,
        context_window=model.context_window,
        max_output_tokens=model.max_output_tokens,
        fallback_key=fallback_key,
        fallback_context_window=fallback_context_window,
        known_ids=frozenset(by_key),
        registry_managed=True,
        reasoning_levels=frozenset(str(level).strip().lower() for level in (model.supported_reasoning_levels or [])),
    )


async def _resolve_from_gateway(requested_key: str | None) -> ChatModelSnapshot:
    """旧版网关路径的解析：列表来自缓存，不碰数据库（凭据与地址由环境变量兜底）。

    兜底时白名单收敛到 `settings.model_name` 与 `FALLBACK_MODEL_NAME` 而不放行任意模型名：
    否则网关 `/v1/models` 抖一下，任意登录用户就能用服务端 key 调未启用的高价模型。
    """
    result, is_fallback = await gateway._load_models(use_cache=True)
    key = requested_key or settings.model_name
    fallback_key = _configured_fallback_name(key)

    if is_fallback:
        allowed = {settings.model_name}
        if fallback_key is not None:
            allowed.add(fallback_key)
        known_ids: frozenset[str] | None = frozenset(allowed)
    else:
        known_ids = frozenset(model.id for model in result.models)

    def limits_of(model_id: str) -> tuple[int | None, int | None]:
        if is_fallback:
            # 兜底条目刻意不伪造窗口
            return None, None
        for model in result.models:
            if model.id == model_id:
                return model.context_window, model.max_output_tokens
        return None, None

    context_window, max_output_tokens = limits_of(key)
    fallback_context_window = limits_of(fallback_key)[0] if fallback_key is not None else None

    # 兜底时列表不可信，思考等级不做校验（空集合 = 无法判定）。
    reasoning_levels: frozenset[str] = frozenset()
    if not is_fallback:
        for model in result.models:
            if model.id == key:
                reasoning_levels = frozenset(str(level).strip().lower() for level in model.supported_reasoning_levels)
                break

    return ChatModelSnapshot(
        key=key,
        target=None,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        fallback_key=fallback_key,
        fallback_context_window=fallback_context_window,
        known_ids=known_ids,
        registry_managed=False,
        reasoning_levels=reasoning_levels,
    )


def _default_key_of(rows: list[tuple[ProviderModel, ModelProvider]]) -> str | None:
    """启用模型里的默认路由键：优先 `is_default`，否则取展示顺序第一条。"""
    for model, provider in rows:
        if model.is_default:
            return make_model_key(provider.slug, model.model_id)
    if rows:
        model, provider = rows[0]
        return make_model_key(provider.slug, model.model_id)
    return None


async def _public_models() -> ModelListResponse:
    async with async_session_maker() as session:
        rows = await enabled_model_rows(session)

    if not rows:
        return ModelListResponse(models=[], default_model="")

    items: list[ModelItem] = []
    explicit_default: str | None = None
    for model, provider in rows:
        key = make_model_key(provider.slug, model.model_id)
        if model.is_default and explicit_default is None:
            explicit_default = key
        items.append(
            ModelItem(
                id=key,
                name=model.display_name,
                provider=provider.name,
                model_id=model.model_id,
                description=model.description or f"{provider.name} {model.display_name}",
                reasoning=model.reasoning,
                supported_reasoning_levels=list(model.supported_reasoning_levels or []),
                context_window=model.context_window,
                max_output_tokens=model.max_output_tokens,
                is_default=False,
            )
        )

    default_key = explicit_default or items[0].id
    for item in items:
        item.is_default = item.id == default_key

    return ModelListResponse(models=items, default_model=default_key)


async def fetch_available_models(*, use_cache: bool = True) -> ModelListResponse:
    """获取可用模型列表：库已接管时只看后台开关（不缓存），否则走旧版网关。"""
    if await provider_config_exists():
        return await _public_models()

    result, _ = await gateway._load_models(use_cache=use_cache)
    return result


async def get_default_model_key() -> str | None:
    """当前默认模型的路由键。

    无提供商配置时返回 `MODEL_NAME`；有配置但没有启用模型时返回 None，调用方应明确拒绝而不是回退环境变量。
    """
    async with async_session_maker() as session:
        if not await registry_configured(session):
            return settings.model_name

        rows = await enabled_model_rows(session)

    return _default_key_of(rows)


async def get_known_model_ids() -> set[str]:
    """可用模型的路由键集合，任何路径都不返回 None。

    库路径下一个模型都没启用也是确定的空集合，调用方必须拒绝所有显式模型。
    旧网关兜底时收敛为 `MODEL_NAME` / `FALLBACK_MODEL_NAME`，否则上游一抖动任意模型名都能用服务端 key。
    """
    if await provider_config_exists():
        async with async_session_maker() as session:
            rows = await enabled_model_rows(session)
        return {make_model_key(provider.slug, model.model_id) for model, provider in rows}

    result, is_fallback = await gateway._load_models(use_cache=True)
    if is_fallback:
        allowed = {settings.model_name}
        fallback_name = _configured_fallback_name(settings.model_name)
        if fallback_name is not None:
            allowed.add(fallback_name)
        return allowed
    return {model.id for model in result.models}


async def get_model_limits(model_id: str) -> tuple[int | None, int | None]:
    """取某个模型的 `(context_window, max_output_tokens)`。

    返回 None 的语义是「无法判定」，调用方（上下文预算）据此放行而不是猜默认窗口：兜底网关条目不伪造窗口，
    未知 id 也已被白名单放行过。库路径按启用状态查找；对话路径改用 `resolve_chat_model`，本函数留给 token_budget。
    """
    if await provider_config_exists():
        async with async_session_maker() as session:
            rows = await enabled_model_rows(session)
        for model, provider in rows:
            if make_model_key(provider.slug, model.model_id) == model_id:
                return model.context_window, model.max_output_tokens
        return None, None

    result, is_fallback = await gateway._load_models(use_cache=True)
    if is_fallback:
        return None, None

    for model in result.models:
        if model.id == model_id:
            return model.context_window, model.max_output_tokens
    return None, None


async def resolve_model_target(key: str) -> ModelTarget | None:
    """把对外路由键解析成完整调用目标；不存在/已停用时返回 None。

    不带白名单与窗口上限，且每次都要自己查库：一个请求里同时要目标和预算时应改用 `resolve_chat_model`。
    """
    async with async_session_maker() as session:
        rows = await enabled_model_rows(session)

    match: tuple[ProviderModel, ModelProvider] | None = None
    for model, provider in rows:
        if make_model_key(provider.slug, model.model_id) == key:
            match = (model, provider)
            break

    if match is None:
        return None

    target = await _build_target(*match)
    fallback_name = _configured_fallback_name(target.upstream_model)
    if fallback_name is None:
        return target

    for model, provider in rows:
        if provider.id == match[1].id and model.model_id == fallback_name:
            return replace(target, fallback=await _build_target(model, provider))

    warn_fallback_unavailable(
        target.upstream_model,
        fallback_name,
        "同一提供商内没有这个已启用的模型（跨提供商的参数兼容性无法保证，因此不做跨提供商兜底）",
    )
    return target
