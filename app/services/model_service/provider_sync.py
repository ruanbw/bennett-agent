"""提供商/模型的写用例与上游同步：自己开会话，出站校验、错误翻译与引导都在这一层。

仓储（`provider_repo`）只管行级读写，所以「建/改提供商要不要拦地址、撞唯一约束报什么错」这类
判定必须留在用例里——它们决定管理员看到的是 400 还是 500。
"""

import ipaddress
import logging
import uuid
from urllib.parse import urlsplit

from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import ModelProvider, ProviderModel, async_session_maker
from app.services.http_client import get_http_client
from app.services.model_service import provider_repo
from app.services.model_service.errors import (
    ApiKeyRequiredError,
    DuplicateModelError,
    InvalidModelStateError,
    ProviderFetchError,
    ProviderNotFoundError,
)
from app.services.model_service.gateway import build_models_url
from app.services.model_service.outbound_guard import (
    _is_metadata_range,
    _warn_plaintext_key_over_public_http,
    reject_private_target,
)
from app.services.model_service.presets import AUTH_STYLES, get_preset
from app.services.model_service.upstream import UpstreamModel, next_page_cursor, parse_upstream_models

logger = logging.getLogger(__name__)

#: 管理后台的「测试连接 / 同步」是显式动作，可以等久一点，但不能无限挂住。
PROVIDER_SYNC_TIMEOUT_SECONDS = 15.0

#: 同步翻页上限：触顶说明上游 `has_more` 恒为 true，宁可截断留日志也不无限打下去。
SYNC_MAX_PAGES = 20

DEFAULT_PROVIDER_NAME = "默认网关"

#: 「未提供」哨兵；不能拿 None 表示不改，管理员需要能把 API key 清空。
_UNSET = object()

#: 引导地址必须带 http(s) scheme：写成 `localhost:4000` 时 httpx 在首次请求前抛
#: `UnsupportedProtocol`，而用户看到的只是「模型不可用」。
_BOOTSTRAP_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _default_display_name(model_id: str) -> str:
    """上游没给展示名时取 id 最后一段（`openai/gpt-4o` -> `gpt-4o`）。"""
    return model_id.rsplit("/", 1)[-1] or model_id


def build_auth_headers(provider: ModelProvider) -> dict[str, str]:
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


async def list_providers() -> list[ModelProvider]:
    async with async_session_maker() as session:
        return await provider_repo.list_providers(session)


async def get_provider(provider_id: uuid.UUID) -> ModelProvider | None:
    async with async_session_maker() as session:
        return await provider_repo.load_provider(session, provider_id)


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

    await reject_private_target(resolved_base_url)
    async with async_session_maker() as session:
        try:
            # slug 的唯一约束在 insert 的 flush 或 commit 处才抛，两处都要在这个 try 里。
            provider = await provider_repo.insert_provider(
                session,
                name=name.strip(),
                slug=await provider_repo.unique_provider_slug(session, name),
                base_url=resolved_base_url,
                api_key=(api_key or None),
                auth_style=resolved_auth,
                extra_headers=resolved_headers,
                preset_key=preset.key if preset else None,
                enabled=enabled,
            )
            # 首次创建即视为「后台已接管」：之后删光提供商也不回退环境变量。
            await provider_repo.mark_registry_initialized(session)
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            logger.warning("创建提供商撞唯一约束（可能是并发创建同名提供商）: name=%s", name)
            raise ProviderFetchError("提供商名称与另一个正在创建的提供商冲突，请重试") from exc

        loaded = await provider_repo.load_provider(session, provider.id)
        if loaded is None:
            # 理论上不可能（刚提交完）；用 assert 会被 `python -O` 删掉，错误点被推到调用方。
            raise ProviderFetchError("创建提供商后无法重新读取，请重试")

    await _warn_plaintext_key_over_public_http(
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
        provider = await provider_repo.load_provider(session, provider_id)
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
                await reject_private_target(next_base_url)
                provider.base_url = next_base_url
        if api_key is not _UNSET:
            # `str(None)` 会写出字符串 "None" 并被当成真实 key 发给上游。
            provider.api_key = (str(api_key).strip() or None) if api_key else None
        if enabled is not _UNSET:
            provider.enabled = bool(enabled)

        # 任何后台写操作都算「后台已接管」：只靠 create/seed 写标记的话，标记上线前
        # 就配置过的旧库在删光提供商后重启会被环境变量重新引导出一条「默认网关」。
        await provider_repo.mark_registry_initialized(session)
        await session.commit()
        loaded = await provider_repo.load_provider(session, provider_id)

    if loaded is not None and base_url is not _UNSET:
        await _warn_plaintext_key_over_public_http(
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
        if not await provider_repo.delete_provider(session, provider_id):
            return False
        await provider_repo.mark_registry_initialized(session)
        await session.commit()
        return True


async def list_provider_models(provider_id: uuid.UUID) -> list[ProviderModel]:
    async with async_session_maker() as session:
        return await provider_repo.list_provider_models(session, provider_id)


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
        if not await provider_repo.provider_exists(session, provider_id):
            raise ProviderNotFoundError(str(provider_id))
        duplicate = await provider_repo.find_duplicate_model_id(session, provider_id, cleaned)
        if duplicate is not None:
            raise DuplicateModelError(cleaned)
        if is_default and not enabled:
            raise InvalidModelStateError("停用的模型不能设为默认模型")
        if is_default:
            await provider_repo.clear_default_model(session)

        model = await provider_repo.insert_model(
            session,
            provider_id=provider_id,
            model_id=cleaned,
            display_name=(display_name or "").strip() or _default_display_name(cleaned),
            description=description,
            reasoning=reasoning,
            supported_reasoning_levels=supported_reasoning_levels,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            enabled=enabled,
            is_default=is_default,
        )
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
        model = await provider_repo.get_model(session, model_uuid)
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
                await provider_repo.clear_default_model(session, keep_id=model.id)
                model.is_default = True
            else:
                model.is_default = False

        if not model.enabled:
            model.is_default = False

        await session.commit()
        return model


async def delete_provider_model(model_uuid: uuid.UUID) -> bool:
    async with async_session_maker() as session:
        if not await provider_repo.delete_model(session, model_uuid):
            return False
        await session.commit()
        return True


async def _fetch_remote_models(provider: ModelProvider) -> list[UpstreamModel]:
    """向单个提供商请求模型列表并解析，失败抛 `ProviderFetchError`。

    信封别名由 `upstream.parse_upstream_models` 兼容，分页只认 Anthropic 形状（`has_more` + `last_id`）。
    错误文案只给管理员看、不含上游响应体（可能含内部信息），只保留状态码与异常类型。
    """
    # 先检查目标地址，再组装认证头——这些请求带着明文 API Key
    await reject_private_target(provider.base_url)

    headers = build_auth_headers(provider)
    models_url = build_models_url(provider.base_url)
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
        existing_rows = await provider_repo.list_provider_models(session, provider_id)
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
                await provider_repo.insert_model(
                    session,
                    provider_id=provider_id,
                    model_id=item.model_id,
                    display_name=(item.display_name or _default_display_name(item.model_id))[:200],
                    description=item.description,
                    reasoning=item.reasoning,
                    supported_reasoning_levels=item.supported_reasoning_levels,
                    context_window=item.context_window,
                    max_output_tokens=item.max_output_tokens,
                    raw=dict(item.raw) if item.raw else None,
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


def validate_bootstrap_base_url(raw: str | None) -> tuple[str | None, str]:
    """校验 `LITELLM_API_BASE` 能否用来引导提供商，返回 `(地址, 不可用原因)`。

    刻意不联网：出站守卫（`outbound_guard.reject_private_target`）发请求时会按当时解析结果再拦一次。
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
    if _is_metadata_range(address):
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
        if await provider_repo.registry_configured(session):
            return False

        provider = await provider_repo.insert_provider(
            session,
            name=DEFAULT_PROVIDER_NAME,
            slug=await provider_repo.unique_provider_slug(session, DEFAULT_PROVIDER_NAME),
            base_url=base_url,
            api_key=settings.litellm_api_key,
            auth_style="bearer",
            extra_headers={},
            preset_key=None,
            enabled=True,
        )

        model_id = (settings.model_name or "").strip() or "openai/gpt-4o"
        await provider_repo.insert_model(
            session,
            provider_id=provider.id,
            model_id=model_id,
            display_name=_default_display_name(model_id),
            description=f"{DEFAULT_PROVIDER_NAME} {_default_display_name(model_id)}",
            is_default=True,
        )
        await provider_repo.mark_registry_initialized(session)
        await session.commit()

    logger.info("已从环境变量引导默认模型提供商: base_url=%s model=%s", base_url, model_id)
    return True
