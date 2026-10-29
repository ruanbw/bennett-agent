"""模型注册表：提供商/模型 CRUD、上游同步，以及对话请求的模型解析。

有任一提供商行或 registry 标记后数据库即唯一权威：禁用立即生效，故不做任何缓存。
预设只在创建时抄进库、运行时只读库，代码更新不得改变已建提供商的行为。
"""

import ipaddress
import logging
import re
import uuid
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db import AppSetting, ModelProvider, ProviderModel, async_session_maker
from app.schemas.chat import ModelItem, ModelListResponse
from app.services.fallback_config import configured_fallback_name, warn_fallback_unavailable
from app.services.http_client import get_http_client
from app.services.model_service import gateway, outbound_guard
from app.services.model_service.errors import (
    ApiKeyRequiredError,
    DuplicateModelError,
    InvalidModelStateError,
    ProviderFetchError,
    ProviderNotFoundError,
)
from app.services.model_service.presets import AUTH_STYLES, get_preset
from app.services.model_service.upstream import (
    UpstreamModel,
    next_page_cursor,
    parse_upstream_models,
)
from app.services.model_target import ModelTarget

logger = logging.getLogger(__name__)

#: 管理后台的「测试连接 / 同步」是显式动作，可以等久一点，但不能无限挂住。
PROVIDER_SYNC_TIMEOUT_SECONDS = 15.0

#: 同步翻页上限：触顶说明上游 `has_more` 恒为 true，宁可截断留日志也不无限打下去。
SYNC_MAX_PAGES = 20

DEFAULT_PROVIDER_NAME = "默认网关"

#: 初始化标记，写入后永不清除：删光提供商仍维持「数据库是权威」。
REGISTRY_MARKER_KEY = "model_registry_initialized"

#: 「未提供」哨兵；不能拿 None 表示不改，管理员需要能把 API key 清空。
_UNSET = object()


def slugify_provider_name(name: str) -> str:
    """把提供商名称转成路由键前缀；非 ASCII 名称退回 `p<随机>`，保证非空唯一。

    slug 只生成一次，管理员改名不会改它。
    """
    base = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not base:
        return f"p{uuid.uuid4().hex[:8]}"
    return base[:48]


def make_model_key(provider_slug_value: str, model_id: str) -> str:
    """对外路由键：`{provider.slug}/{model_id}`。"""
    return f"{provider_slug_value}/{model_id}"


def _default_display_name(model_id: str) -> str:
    """上游没给展示名时取 id 最后一段（`openai/gpt-4o` -> `gpt-4o`）。"""
    return model_id.rsplit("/", 1)[-1] or model_id


async def _provider_config_exists(session) -> bool:  # noqa: ANN001 - AsyncSession 只在内部使用
    """数据库配置是否已经接管模型清单：有 registry 标记或有提供商行都算（兼容标记上线前的库）。

    只有全新库且从未配置才返回 False，此时退回旧版环境变量网关。
    """
    if await session.get(AppSetting, REGISTRY_MARKER_KEY) is not None:
        return True
    return await session.scalar(select(ModelProvider.id).limit(1)) is not None


async def _mark_registry_initialized(session) -> None:  # noqa: ANN001
    marker = await session.get(AppSetting, REGISTRY_MARKER_KEY)
    if marker is None:
        session.add(AppSetting(key=REGISTRY_MARKER_KEY, value="1"))
    else:
        marker.value = "1"


async def provider_config_exists() -> bool:
    """对外的 `_provider_config_exists`：True 表示管理后台是权威（即使此刻零个提供商）。"""
    async with async_session_maker() as session:
        return await _provider_config_exists(session)


async def _load_provider(session, provider_id: uuid.UUID) -> ModelProvider | None:  # noqa: ANN001
    stmt = select(ModelProvider).where(ModelProvider.id == provider_id).options(selectinload(ModelProvider.models))
    return await session.scalar(stmt)


async def _unique_provider_slug(session, name: str) -> str:  # noqa: ANN001
    base = slugify_provider_name(name)
    candidate = base
    index = 2
    while await session.scalar(select(ModelProvider.id).where(ModelProvider.slug == candidate)) is not None:
        candidate = f"{base}-{index}"[:60]
        index += 1
    return candidate


async def _enabled_rows(session) -> list[tuple[ProviderModel, ModelProvider]]:  # noqa: ANN001
    """所有「提供商启用 + 模型启用」的行，按后台展示顺序。"""
    stmt = (
        select(ProviderModel, ModelProvider)
        .join(ModelProvider, ProviderModel.provider_id == ModelProvider.id)
        .where(ProviderModel.enabled.is_(True), ModelProvider.enabled.is_(True))
        .order_by(ModelProvider.created_at, ProviderModel.sort_order, ProviderModel.display_name)
    )
    rows = (await session.execute(stmt)).all()
    return [(row[0], row[1]) for row in rows]


async def _build_target(model: ProviderModel, provider: ModelProvider) -> ModelTarget:
    """组装一次调用目标并在出站前校验地址；这里是唯一构造带 `base_url` 的 ModelTarget 处。

    地址非法抛 `ProviderFetchError`：返回 None 会变成「模型已停用」的误导提示。
    不缓存解析结果，否则 DNS rebinding 的窗口会从单次请求拉到整个进程生命周期。
    """
    await outbound_guard._reject_private_target(provider.base_url)
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


async def resolve_chat_model(requested_key: str | None) -> ChatModelSnapshot:
    """解析本轮对话的模型：白名单 + 目标 + 窗口/输出上限，一次读库。

    `requested_key` 为 None 取后台默认；库路径下未知或已停用的模型返回 `target=None`，调用方必须拒绝而不是退回环境变量。
    备用模型只在同一提供商内找：跨提供商的参数兼容性无法保证（如思考等级词表不同）。
    """
    async with async_session_maker() as session:
        if not await _provider_config_exists(session):
            rows: list[tuple[ProviderModel, ModelProvider]] = []
            registry_managed = False
        else:
            rows = await _enabled_rows(session)
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


#: 引导地址必须带 http(s) scheme：写成 `localhost:4000` 时 httpx 在首次请求前抛
#: `UnsupportedProtocol`，而用户看到的只是「模型不可用」。
_BOOTSTRAP_ALLOWED_SCHEMES = frozenset({"http", "https"})


def validate_bootstrap_base_url(raw: str | None) -> tuple[str | None, str]:
    """校验 `LITELLM_API_BASE` 能否用来引导提供商，返回 `(地址, 不可用原因)`。

    刻意不联网：出站守卫（`outbound_guard._reject_private_target`）发请求时会按当时解析结果再拦一次。
    必须先于写库：写入后库即权威、环境变量路径停用，一行笔误就把新实例变成零可用模型。
    """
    # 先校验原值再归一化：`http://` 去掉尾斜杠会变成 `http:`，报错里就看不出问题。
    original = (raw or "").strip()
    if not original:
        return None, "LITELLM_API_BASE 为空"

    parsed = urlsplit(original)
    if parsed.scheme.lower() not in _BOOTSTRAP_ALLOWED_SCHEMES:
        return None, f"LITELLM_API_BASE 必须以 http:// 或 https:// 开头（收到 {original!r}）"
    if not parsed.hostname:
        return None, f"LITELLM_API_BASE 缺少主机名（收到 {original!r}）"
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        return None, f"LITELLM_API_BASE 端口不合法（收到 {original!r}）"

    base_url = original.rstrip("/")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return base_url, ""
    if outbound_guard._is_metadata_range(address):
        return None, f"LITELLM_API_BASE 指向链路本地/未指定地址（{address}），不能作为模型网关"
    return base_url, ""


async def seed_default_provider() -> bool:
    """首次启动时把环境变量里的网关引导成一条提供商记录，返回是否真的写入。

    只写库、不访问上游：启动不应因网关不可达而变慢或失败，模型列表等管理员点「同步模型」。
    """
    if not settings.bootstrap_provider_from_env:
        return False

    base_url, reason = validate_bootstrap_base_url(settings.litellm_api_base)
    if base_url is None:
        logger.warning(
            "跳过从环境变量引导模型提供商：%s。目前仍按环境变量网关工作，"
            "修正 LITELLM_API_BASE 后重启，或直接到管理后台添加提供商。",
            reason,
        )
        return False

    async with async_session_maker() as session:
        if await _provider_config_exists(session):
            return False

        provider = ModelProvider(
            name=DEFAULT_PROVIDER_NAME,
            slug=await _unique_provider_slug(session, DEFAULT_PROVIDER_NAME),
            base_url=base_url,
            api_key=settings.litellm_api_key,
            enabled=True,
        )
        session.add(provider)
        await session.flush()

        model_id = (settings.model_name or "").strip() or "openai/gpt-4o"
        session.add(
            ProviderModel(
                provider_id=provider.id,
                model_id=model_id,
                display_name=_default_display_name(model_id),
                description=f"{DEFAULT_PROVIDER_NAME} {_default_display_name(model_id)}",
                enabled=True,
                is_default=True,
            )
        )
        await _mark_registry_initialized(session)
        await session.commit()

    logger.info("已从环境变量引导默认模型提供商: base_url=%s model=%s", base_url, model_id)
    return True


async def list_providers() -> list[ModelProvider]:
    """全部提供商（含禁用的），带已加载的模型列表（用于计数与展示）。"""
    async with async_session_maker() as session:
        stmt = (
            select(ModelProvider)
            .options(selectinload(ModelProvider.models))
            .order_by(ModelProvider.created_at, ModelProvider.name)
        )
        return list((await session.scalars(stmt)).all())


async def get_provider(provider_id: uuid.UUID) -> ModelProvider | None:
    async with async_session_maker() as session:
        return await _load_provider(session, provider_id)


async def create_provider(
    *,
    name: str,
    base_url: str | None = None,
    api_key: str | None = None,
    enabled: bool = True,
    preset_key: str | None = None,
    auth_style: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> ModelProvider:
    """新建提供商；`preset_key` 指向内置预设时地址与鉴权细节可省略，预设值只在创建时抄进库。

    地址当场校验（填错内网/元数据地址要马上看到 400）；并发建同名提供商会撞 slug 唯一约束，
    裸 `IntegrityError` 是 500，这里翻译成可读的重试提示。
    """
    preset = get_preset(preset_key) if preset_key else None
    if preset_key and preset is None:
        raise ProviderFetchError(f"未知的提供商预设：{preset_key}")

    resolved_base_url = (base_url or "").strip().rstrip("/") or (preset.base_url if preset else "")
    if not resolved_base_url:
        raise ProviderFetchError("必须填写 Base URL，或选择一个厂商预设")

    resolved_auth = auth_style or (preset.auth_style if preset else "bearer")
    if resolved_auth not in AUTH_STYLES:
        raise ProviderFetchError(f"未知的认证方式：{resolved_auth}")
    resolved_headers = (
        {str(k): str(v) for k, v in extra_headers.items() if v is not None}
        if extra_headers is not None
        else dict(preset.extra_headers if preset else {})
    )

    await outbound_guard._reject_private_target(resolved_base_url)
    async with async_session_maker() as session:
        provider = ModelProvider(
            name=name.strip(),
            slug=await _unique_provider_slug(session, name),
            base_url=resolved_base_url,
            api_key=(api_key or None),
            auth_style=resolved_auth,
            extra_headers=resolved_headers,
            preset_key=preset.key if preset else None,
            enabled=enabled,
        )
        session.add(provider)
        try:
            # 首次创建即视为「后台已接管」：之后删光提供商也不回退环境变量。
            # 必须和 commit 在同一个 try 里：`session.get` 触发 autoflush，slug 撞约束在那里就抛。
            await _mark_registry_initialized(session)
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            logger.warning("创建提供商撞唯一约束（可能是并发创建同名提供商）: name=%s", name)
            raise ProviderFetchError("提供商名称与另一个正在创建的提供商冲突，请重试") from exc

        loaded = await _load_provider(session, provider.id)
        if loaded is None:
            # 理论上不可能（刚提交完）；用 assert 会被 `python -O` 删掉，错误点被推到调用方。
            raise ProviderFetchError("创建提供商后无法重新读取，请重试")

    await outbound_guard._warn_plaintext_key_over_public_http(
        loaded.base_url,
        provider_name=loaded.name,
        has_api_key=bool(loaded.api_key),
    )
    return loaded


async def update_provider(
    provider_id: uuid.UUID,
    *,
    name: object = _UNSET,
    base_url: object = _UNSET,
    api_key: object = _UNSET,
    enabled: object = _UNSET,
) -> ModelProvider | None:
    """更新提供商；`api_key` 传 `None` 表示清空，不传则保持不变。

    换地址等于换凭据：新地址的主人能在下次「测试连接」收到库里那份明文 key，所以有 key 时改地址必须重填。
    只改名字/启用状态可以不填（前端每次保存都原样回传 base_url），所以比的是地址是否变化；无 key 不拦。
    """
    async with async_session_maker() as session:
        provider = await _load_provider(session, provider_id)
        if provider is None:
            return None

        if name is not _UNSET:
            provider.name = str(name).strip()
        if base_url is not _UNSET:
            # 只校验变化过的新地址：不动地址的保存不该因旧行违规而失败（那样由 `_build_target` 出站时拦）。
            next_base_url = str(base_url).strip().rstrip("/")
            if next_base_url != provider.base_url:
                if provider.api_key and api_key is _UNSET:
                    raise ApiKeyRequiredError("更换 Base URL 时必须重新填写 API Key")
                await outbound_guard._reject_private_target(next_base_url)
                provider.base_url = next_base_url
        if api_key is not _UNSET:
            # `str(None)` 会写出字符串 "None" 并被当成真实 key 发给上游。
            provider.api_key = (str(api_key).strip() or None) if api_key else None
        if enabled is not _UNSET:
            provider.enabled = bool(enabled)

        # 任何后台写操作都算「后台已接管」：只靠 create/seed 写标记的话，标记上线前
        # 就配置过的旧库在删光提供商后重启会被环境变量重新引导出一条「默认网关」。
        await _mark_registry_initialized(session)
        await session.commit()
        loaded = await _load_provider(session, provider_id)

    if loaded is not None and base_url is not _UNSET:
        await outbound_guard._warn_plaintext_key_over_public_http(
            loaded.base_url,
            provider_name=loaded.name,
            has_api_key=bool(loaded.api_key),
        )
    return loaded


async def delete_provider(provider_id: uuid.UUID) -> bool:
    """删除提供商及其全部模型。

    同样写 registry 标记：删光是管理员的明确意图，不代表回到出厂状态；否则旧库删完重启会被
    `seed_default_provider` 用 `LITELLM_API_BASE` 重建一条。只想停用请用 `enabled=False`。
    """
    async with async_session_maker() as session:
        provider = await session.get(ModelProvider, provider_id)
        if provider is None:
            return False
        # 显式删子表：不把数据完整性压在外键级联上
        await session.execute(delete(ProviderModel).where(ProviderModel.provider_id == provider_id))
        await session.delete(provider)
        await _mark_registry_initialized(session)
        await session.commit()
        return True


async def list_provider_models(provider_id: uuid.UUID) -> list[ProviderModel]:
    async with async_session_maker() as session:
        stmt = (
            select(ProviderModel)
            .where(ProviderModel.provider_id == provider_id)
            .order_by(ProviderModel.sort_order, ProviderModel.display_name)
        )
        return list((await session.scalars(stmt)).all())


async def _find_duplicate_model_id(session, provider_id: uuid.UUID, model_id: str) -> uuid.UUID | None:  # noqa: ANN001
    """预检同一提供商下是否已有同名模型。

    单列成函数是为了让测试能把「先查」替换成查不到：并发写入时唯一约束才是裁决者，那条路径要能单独验证。
    """
    return await session.scalar(
        select(ProviderModel.id).where(
            ProviderModel.provider_id == provider_id,
            ProviderModel.model_id == model_id,
        )
    )


async def create_provider_model(
    provider_id: uuid.UUID,
    *,
    model_id: str,
    display_name: str | None = None,
    description: str | None = None,
    reasoning: bool | None = None,
    supported_reasoning_levels: list[str] | None = None,
    context_window: int | None = None,
    max_output_tokens: int | None = None,
    enabled: bool = True,
    is_default: bool = False,
) -> ProviderModel:
    """手动添加一个模型（同步上游之外的补充途径）。

    并发添加同一 `model_id` 会撞 `uq_provider_model`，翻译成 `DuplicateModelError`（路由层已映射 409）。
    """
    cleaned = model_id.strip()
    async with async_session_maker() as session:
        if await session.get(ModelProvider, provider_id) is None:
            raise ProviderNotFoundError(str(provider_id))
        duplicate = await _find_duplicate_model_id(session, provider_id, cleaned)
        if duplicate is not None:
            raise DuplicateModelError(cleaned)
        if is_default and not enabled:
            raise InvalidModelStateError("停用的模型不能设为默认模型")
        if is_default:
            await session.execute(update(ProviderModel).values(is_default=False))

        model = ProviderModel(
            provider_id=provider_id,
            model_id=cleaned,
            display_name=(display_name or "").strip() or _default_display_name(cleaned),
            description=description,
            reasoning=reasoning,
            supported_reasoning_levels=list(supported_reasoning_levels or []),
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            enabled=enabled,
            is_default=is_default,
        )
        session.add(model)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            logger.warning("添加模型撞唯一约束（可能是并发添加同一模型）: provider=%s model=%s", provider_id, cleaned)
            raise DuplicateModelError(cleaned) from exc
        return model


async def update_provider_model(
    model_uuid: uuid.UUID,
    *,
    display_name: object = _UNSET,
    description: object = _UNSET,
    reasoning: object = _UNSET,
    supported_reasoning_levels: object = _UNSET,
    context_window: object = _UNSET,
    max_output_tokens: object = _UNSET,
    enabled: object = _UNSET,
    is_default: object = _UNSET,
) -> ProviderModel | None:
    """更新模型字段；切换默认时全局只保留一个默认模型。"""
    async with async_session_maker() as session:
        model = await session.get(ProviderModel, model_uuid)
        if model is None:
            return None

        if display_name is not _UNSET:
            model.display_name = str(display_name).strip() or _default_display_name(model.model_id)
        if description is not _UNSET:
            model.description = str(description) if description else None
        if reasoning is not _UNSET:
            model.reasoning = bool(reasoning) if reasoning is not None else None
        if supported_reasoning_levels is not _UNSET:
            model.supported_reasoning_levels = list(supported_reasoning_levels or [])
        if context_window is not _UNSET:
            model.context_window = int(context_window) if context_window is not None else None
        if max_output_tokens is not _UNSET:
            model.max_output_tokens = int(max_output_tokens) if max_output_tokens is not None else None
        if enabled is not _UNSET:
            model.enabled = bool(enabled)

        if is_default is not _UNSET:
            if is_default:
                if not model.enabled:
                    raise InvalidModelStateError("停用的模型不能设为默认模型")
                await session.execute(
                    update(ProviderModel).where(ProviderModel.id != model.id).values(is_default=False)
                )
                model.is_default = True
            else:
                model.is_default = False

        if not model.enabled:
            model.is_default = False

        await session.commit()
        return model


async def delete_provider_model(model_uuid: uuid.UUID) -> bool:
    async with async_session_maker() as session:
        model = await session.get(ProviderModel, model_uuid)
        if model is None:
            return False
        await session.delete(model)
        await session.commit()
        return True


def _auth_headers(provider: ModelProvider) -> dict[str, str]:
    """组装出站请求头：固定头在前，API Key 按库里的 `auth_style` 放进对应位置。"""
    headers = {str(key): str(value) for key, value in (provider.extra_headers or {}).items()}
    if provider.api_key and provider.auth_style != "none":
        if provider.auth_style == "x-api-key":
            headers["x-api-key"] = provider.api_key
        elif provider.auth_style == "x-goog-api-key":
            headers["x-goog-api-key"] = provider.api_key
        else:
            headers["Authorization"] = f"Bearer {provider.api_key}"
    return headers


async def _fetch_remote_models(provider: ModelProvider) -> list[UpstreamModel]:
    """向单个提供商请求模型列表并解析，失败抛 `ProviderFetchError`。

    信封别名由 `upstream.parse_upstream_models` 兼容，分页只认 Anthropic 形状（`has_more` + `last_id`）。
    错误文案只给管理员看、不含上游响应体（可能含内部信息），只保留状态码与异常类型。
    """
    # 先检查目标地址，再组装认证头——这些请求带着明文 API Key
    await outbound_guard._reject_private_target(provider.base_url)

    headers = _auth_headers(provider)
    models_url = gateway._build_models_url(provider.base_url)
    client = get_http_client()

    entries: list[UpstreamModel] = []
    cursor: str | None = None
    for page in range(SYNC_MAX_PAGES):
        try:
            response = await client.get(
                models_url,
                headers=headers,
                params={"after_id": cursor} if cursor else None,
                timeout=PROVIDER_SYNC_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - 任何网络/超时/URL 错误都归为「连不上」
            logger.warning("拉取提供商模型列表失败 (provider=%s): %s", provider.name, exc, exc_info=True)
            raise ProviderFetchError(f"无法连接提供商（{type(exc).__name__}）") from exc

        if response.status_code in (401, 403):
            raise ProviderFetchError("认证失败，请检查 API Key")
        if response.status_code == 404:
            raise ProviderFetchError("模型列表地址不存在，请检查 Base URL 是否需要包含 /v1")
        if response.status_code != 200:
            raise ProviderFetchError(f"提供商返回 HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderFetchError("提供商返回的不是 JSON") from exc

        if isinstance(payload, list):
            remote_entries: list[object] = payload
        elif isinstance(payload, dict) and isinstance(payload.get("data"), list):
            remote_entries = payload["data"]
        elif isinstance(payload, dict) and isinstance(payload.get("models"), list):
            remote_entries = payload["models"]
        else:
            raise ProviderFetchError("提供商响应格式不是 OpenAI /v1/models 结构")

        if not remote_entries and page == 0:
            return []

        entries.extend(parse_upstream_models(payload, settings.model_description_field))
        cursor = next_page_cursor(payload)
        if cursor is None:
            break
    else:
        logger.warning("提供商 %s 的模型列表分页超过 %d 页，结果已截断", provider.name, SYNC_MAX_PAGES)

    # 列宽 200：Postgres 会直接报错，超长 id 也放不进 ChatRequest 的 300 字符上限。跳过并留日志，不静默截断。
    too_long = [entry.model_id for entry in entries if len(entry.model_id) > 200]
    if too_long:
        logger.warning("跳过上游返回的超长模型 id: %s", too_long[:5])
    return [entry for entry in entries if len(entry.model_id) <= 200]


async def test_provider_connection(provider_id: uuid.UUID) -> int:
    """测试提供商可达性，返回上游声明的模型数量。"""
    provider = await get_provider(provider_id)
    if provider is None:
        raise ProviderNotFoundError(str(provider_id))
    return len(await _fetch_remote_models(provider))


async def sync_provider_models(provider_id: uuid.UUID) -> tuple[int, int, int]:
    """把上游模型列表同步进库，返回 `(上游数量, 新增, 更新)`。

    已存在模型的 `enabled` / `is_default` 保持不变；上游不再返回的模型不删除，那可能只是一时不完整。
    上游重复 id 与并发同步都会撞 `uq_provider_model`：循环里跳过重复，提交处把冲突翻译成可重试提示。
    """
    provider = await get_provider(provider_id)
    if provider is None:
        raise ProviderNotFoundError(str(provider_id))

    remote = await _fetch_remote_models(provider)

    async with async_session_maker() as session:
        existing_rows = list(
            (await session.scalars(select(ProviderModel).where(ProviderModel.provider_id == provider_id))).all()
        )
        existing = {row.model_id: row for row in existing_rows}
        #: 本轮已处理的 id：上游重复返回同一 id 时跳过，既不能重复 add 也不该算作更新。
        seen: set[str] = set()
        created = 0
        updated = 0
        duplicated = 0

        for item in remote:
            if item.model_id in seen:
                duplicated += 1
                continue
            seen.add(item.model_id)

            row = existing.get(item.model_id)
            if row is None:
                session.add(
                    ProviderModel(
                        provider_id=provider_id,
                        model_id=item.model_id,
                        display_name=(item.display_name or _default_display_name(item.model_id))[:200],
                        description=item.description,
                        reasoning=item.reasoning,
                        supported_reasoning_levels=list(item.supported_reasoning_levels),
                        context_window=item.context_window,
                        max_output_tokens=item.max_output_tokens,
                        raw=dict(item.raw) if item.raw else None,
                        enabled=True,
                    )
                )
                created += 1
                continue

            row.display_name = (item.display_name or _default_display_name(item.model_id))[:200]
            row.description = item.description
            row.reasoning = item.reasoning
            row.supported_reasoning_levels = list(item.supported_reasoning_levels)
            # 只在非 None 时覆盖：多数厂商 `/v1/models` 不报窗口，无条件赋值会把 NULL 写进手填值，
            # 而 NULL 等于「不知道窗口」，上下文守卫会被永久静默关闭。
            if item.context_window is not None:
                row.context_window = item.context_window
            if item.max_output_tokens is not None:
                row.max_output_tokens = item.max_output_tokens
            row.raw = dict(item.raw) if item.raw else None
            updated += 1

        if duplicated:
            logger.warning("上游模型列表里有 %d 个重复 id，已跳过（provider=%s）", duplicated, provider.name)

        try:
            await session.commit()
        except IntegrityError as exc:
            # 并发同步抢插同一 (provider_id, model_id)：把约束冲突翻译成可重试提示。
            await session.rollback()
            logger.warning("同步提供商模型撞唯一约束（可能是并发同步）: provider=%s", provider.name)
            raise ProviderFetchError("同步与另一次同步冲突，请稍后重试") from exc

    logger.info(
        "同步提供商模型 (provider=%s): 上游=%d 新增=%d 更新=%d",
        provider.name,
        len(remote),
        created,
        updated,
    )
    return len(remote), created, updated


async def _public_models() -> ModelListResponse:
    async with async_session_maker() as session:
        rows = await _enabled_rows(session)

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
        if not await _provider_config_exists(session):
            return settings.model_name

        rows = await _enabled_rows(session)

    return _default_key_of(rows)


async def get_known_model_ids() -> set[str]:
    """可用模型的路由键集合，任何路径都不返回 None。

    库路径下一个模型都没启用也是确定的空集合，调用方必须拒绝所有显式模型。
    旧网关兜底时收敛为 `MODEL_NAME` / `FALLBACK_MODEL_NAME`，否则上游一抖动任意模型名都能用服务端 key。
    """
    if await provider_config_exists():
        async with async_session_maker() as session:
            rows = await _enabled_rows(session)
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
            rows = await _enabled_rows(session)
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
        rows = await _enabled_rows(session)

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
