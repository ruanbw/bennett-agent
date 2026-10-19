"""模型清单服务：管理后台维护的提供商配置 + 旧版网关兜底。

## 两个数据源，一个对外接口

管理后台把「提供商 -> 模型 -> 是否启用」存进数据库（`model_provider` /
`provider_model`）。一旦库里存在**任意**提供商，数据库就是唯一权威：

- `fetch_available_models()` 只返回已启用提供商下的已启用模型；
- `get_known_model_ids()` 返回这些模型的路由键（空集合就是「一个都没有」，
  而不是「无法校验」）；
- 禁用一个模型会立刻从列表消失，对话请求也会被白名单拒绝。

库里一条提供商都没有时（旧部署、测试、尚未引导）才退回原有的「直接读
`LITELLM_API_BASE` 的 `/v1/models`」路径。这条兜底只为了升级不中断，不是第二套
配置入口：首次启动会用环境变量**引导**出一条「默认网关」提供商，之后由后台接管。

## 路由键

对外模型 id 是 `{provider.slug}/{model_id}`。不同提供商可以各自提供 `gpt-4o`，
只用 `model_id` 做键会互相覆盖；`slug` 生成后不随改名变化，用户已保存的选择不会
因为管理员改个名字就指向另一个模型。

## 为什么数据库路径不缓存

模型启停是管理员刚点完就要生效的操作。管理后台的写操作与对话请求不在同一个进程
生命周期里没有通知机制，任何 TTL 缓存都会造成「明明禁用了还能用」。两张表都很小，
每次请求多两条查询比缓存失效逻辑便宜得多。旧版网关路径的缓存保持原样。
"""

import asyncio
import ipaddress
import logging
import re
import socket
import time
import uuid
from dataclasses import replace
from urllib.parse import urlsplit

from sqlalchemy import delete, select, update
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db import AppSetting, ModelProvider, ProviderModel, async_session_maker
from app.schemas.chat import ModelItem, ModelListResponse
from app.services.http_client import get_http_client
from app.services.model_target import ModelTarget

logger = logging.getLogger(__name__)

#: 拉取模型列表是页面加载路径上的一步，3 秒没结果就该走兜底。
MODELS_TIMEOUT_SECONDS = 3.0

#: 管理后台的「测试连接 / 同步模型」是显式动作，可以等久一点；但也不能无限挂住。
PROVIDER_SYNC_TIMEOUT_SECONDS = 15.0

#: 缓存有效期。网关的模型列表变化很慢，而每次页面加载都打上游既慢又浪费。
MODELS_CACHE_TTL_SECONDS = 300.0

#: 兜底结果缓存有效期（负缓存）。避免上游瞬时抖动造成过长时间锁定，同时防止频繁超时。
FALLBACK_CACHE_TTL_SECONDS = 10.0

#: 成功结果的缓存：(写入时刻, 响应)。只有成功结果会写进这一格。
_success_cache: tuple[float, ModelListResponse] | None = None
#: 负缓存：最近一次「上游不可用」产生的兜底响应。刻意与成功缓存分开存——失败永远
#: 顶不掉已有的成功结果（共用一格时，慢失败的兜底会覆盖刚拿到的成功列表）。
_fallback_cache: tuple[float, ModelListResponse] | None = None

#: single-flight 锁，以及它绑定的 loop。锁按事件循环惰性重建：asyncio.Lock 会绑定
#: 首次使用它的 loop，模块级常量锁在「每个用例一个 loop」的测试环境里会 RuntimeError。
_lock: asyncio.Lock | None = None
_lock_loop: asyncio.AbstractEventLoop | None = None


def _build_models_url(base_url: str) -> str:
    """根据提供商的 base_url 拼出模型列表地址。

    兼容两种常见写法：`http://host:4000`（OpenAI SDK 的 base_url 习惯）与
    `http://host:4000/v1`（很多自建网关/官方文档直接给到 /v1）。重复拼 `/v1`
    会让提供商返回 404，而 404 又很容易被误读成「提供商不可用」。
    """
    base = base_url.strip().rstrip("/")
    return f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"


def _build_url() -> str:
    return _build_models_url(settings.litellm_api_base)


def _parse_int_field(value: object) -> int | None:
    """宽松解析 int 型字段：int 或数字串；其它（None、'128k'、浮点……）一律 None。

    刻意不做单位换算：把 '128k' 猜成 128000 是伪造数据，前端会拿着编出来的上下文
    窗口做判断。
    """
    if isinstance(value, bool):
        # bool 是 int 的子类，但 True/False 在这两个字段上没有语义
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _parse_reasoning_flag(item: dict[str, object]) -> bool | None:
    """reasoning 三态：只有网关给出的 bool 才算表态，其余（缺字段、类型不符）为 None。

    None 与 False 必须分开：False 是「网关明确说不支持」，前端据此隐藏思考等级选择器；
    None 是「网关没说」，按未知处理。此前 `bool(item.get("reasoning", False))` 把两者
    压成了同一个 False。
    """
    value = item.get("reasoning")
    return value if isinstance(value, bool) else None


def _parse_reasoning_levels(raw: object) -> list[str]:
    """解析模型支持的思考等级。

    网关的返回有两种形状：`[{"effort": "high"}]` 或 `["high"]`。两种都接受，其它
    形状忽略——伪造等级会让用户选了之后请求失败（`{"effort": null}` 经 `str()` 会
    变成等级 "None"）。空白串不是等级，照样丢掉。
    """
    if not isinstance(raw, list):
        return []

    levels: list[str] = []
    for item in raw:
        value = item.get("effort") if isinstance(item, dict) else item
        if not isinstance(value, str):
            continue
        cleaned = value.strip()
        if cleaned and cleaned not in levels:
            levels.append(cleaned)
    return levels


def _parse_models(
    payload: object,
    default_model_id: str,
    description_field: str,
    *,
    mark_default: bool = True,
) -> ModelListResponse | None:
    """把上游响应转成 `ModelListResponse`；无可用模型时返回 None。

    `description_field` 由配置传入：上游网关可以提供一个比 'provider name' 更友好
    的展示标签，但字段名属于网关细节，不该硬编码在这里。

    `mark_default=False` 用于管理后台的同步：默认模型由数据库行决定，这里不该把
    上游列表的第一项强行标成默认（也不该为「配置的默认模型」打一条误导性日志）。

    容错做在**条目级**：某个模型的字段类型不符（`id` 是数字、`display_name` 是数字……）
    只跳过那一条。此前任何一条抛 ValidationError 都会被外层 except 捕获、整份作废，
    列表于是只剩兜底的那一个模型。
    """
    if not isinstance(payload, dict):
        return None

    data = payload.get("data")
    if not isinstance(data, list):
        return None

    models: list[ModelItem] = []
    found_default = False

    for index, item in enumerate(data):
        if not isinstance(item, dict):
            continue

        try:
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
                    reasoning=_parse_reasoning_flag(item),
                    supported_reasoning_levels=_parse_reasoning_levels(item.get("supported_reasoning_levels", [])),
                    context_window=_parse_int_field(item.get("context_window")),
                    max_output_tokens=_parse_int_field(item.get("max_output_tokens")),
                    is_default=is_default,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 单条坏数据不能作废整份列表
            logger.warning("跳过无法解析的模型条目 (index=%d, id=%r): %s", index, item.get("id"), exc)

    if not models:
        return None

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


# =====================================================================
# 管理后台维护的提供商 / 模型清单
# =====================================================================

DEFAULT_PROVIDER_NAME = "默认网关"

#: 模型注册表初始化标记。写入后永不清除：删光提供商仍然维持「数据库是权威」。
REGISTRY_MARKER_KEY = "model_registry_initialized"


class ProviderNotFoundError(LookupError):
    """提供商不存在（管理接口据此返回 404）。"""


class DuplicateModelError(ValueError):
    """同一提供商下已经存在同名模型。"""


class InvalidModelStateError(ValueError):
    """模型状态组合不合法，例如把已停用的模型设为默认。"""


class ProviderFetchError(RuntimeError):
    """无法从提供商读取模型列表。消息是给管理员看的，不包含上游响应原文。"""


#: 更新接口的「未提供」哨兵。不能拿 None 表示不改：管理员需要能把 API key 清空。
_UNSET = object()


def slugify_provider_name(name: str) -> str:
    """把提供商名称转成路由键前缀。

    名称可能是中文：全部非 ASCII 字符会被剥掉，此时退回 `p<随机>`，保证 slug 非空且
    唯一（由 `_unique_provider_slug` 再补后缀）。slug 只生成一次，改名不会改它。
    """
    base = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not base:
        return f"p{uuid.uuid4().hex[:8]}"
    return base[:48]


def make_model_key(provider_slug_value: str, model_id: str) -> str:
    """对外路由键：`{provider.slug}/{model_id}`。"""
    return f"{provider_slug_value}/{model_id}"


def _default_display_name(model_id: str) -> str:
    """上游没给展示名时的默认值：取命名空间后的最后一段（`openai/gpt-4o` -> `gpt-4o`）。"""
    return model_id.rsplit("/", 1)[-1] or model_id


async def _provider_config_exists(session) -> bool:  # noqa: ANN001 - AsyncSession 只在内部使用
    """数据库配置是否已经**接管**模型清单。

    两种情况都算接管：
    - `app_setting` 里有初始化标记（首次引导 / 首次创建提供商时写入）；
    - 表里已经存在提供商行（兼容标记功能上线前就已配置过的库）。

    反过来，全新库且从未配置时才返回 False，退回旧版环境变量网关。
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
    """数据库配置是否已经接管模型清单（见 `_provider_config_exists`）。

    True 表示管理后台是权威（即使此刻零个提供商）；False 才允许退回环境变量路径。
    """
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


def _build_target(model: ProviderModel, provider: ModelProvider) -> ModelTarget:
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


async def seed_default_provider() -> bool:
    """首次启动时把环境变量里的网关引导成一条提供商记录。

    只写库、不访问上游：启动路径上不应该因为网关不可达而变慢或失败。管理员进入
    后台点一次「同步模型」即可补齐列表；在那之前至少有一个可用的默认模型。

    返回是否真的写入了记录（便于启动日志与测试断言）。
    """
    if not settings.bootstrap_provider_from_env:
        return False

    async with async_session_maker() as session:
        if await _provider_config_exists(session):
            return False

        provider = ModelProvider(
            name=DEFAULT_PROVIDER_NAME,
            slug=await _unique_provider_slug(session, DEFAULT_PROVIDER_NAME),
            base_url=settings.litellm_api_base.strip().rstrip("/"),
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

    logger.info("已从环境变量引导默认模型提供商: base_url=%s model=%s", settings.litellm_api_base, model_id)
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
    base_url: str,
    api_key: str | None = None,
    enabled: bool = True,
) -> ModelProvider:
    """新建提供商。slug 由名称生成并保证唯一。"""
    async with async_session_maker() as session:
        provider = ModelProvider(
            name=name.strip(),
            slug=await _unique_provider_slug(session, name),
            base_url=base_url.strip().rstrip("/"),
            api_key=(api_key or None),
            enabled=enabled,
        )
        session.add(provider)
        # 首次创建提供商即视为「后台已经接管」：之后即使删光提供商，也不回退到环境变量
        await _mark_registry_initialized(session)
        await session.commit()
        loaded = await _load_provider(session, provider.id)
        assert loaded is not None
        return loaded


async def update_provider(
    provider_id: uuid.UUID,
    *,
    name: object = _UNSET,
    base_url: object = _UNSET,
    api_key: object = _UNSET,
    enabled: object = _UNSET,
) -> ModelProvider | None:
    """更新提供商；`api_key` 传 `None` 表示清空，不传则保持不变。"""
    async with async_session_maker() as session:
        provider = await _load_provider(session, provider_id)
        if provider is None:
            return None

        if name is not _UNSET:
            provider.name = str(name).strip()
        if base_url is not _UNSET:
            provider.base_url = str(base_url).strip().rstrip("/")
        if api_key is not _UNSET:
            provider.api_key = str(api_key) if api_key else None
        if enabled is not _UNSET:
            provider.enabled = bool(enabled)

        await session.commit()
        return await _load_provider(session, provider_id)


async def delete_provider(provider_id: uuid.UUID) -> bool:
    """删除提供商及其全部模型。"""
    async with async_session_maker() as session:
        provider = await session.get(ModelProvider, provider_id)
        if provider is None:
            return False
        # 显式删子表：SQLite 的 FK 级联依赖连接级 PRAGMA，这里不把数据完整性压在它上面
        await session.execute(delete(ProviderModel).where(ProviderModel.provider_id == provider_id))
        await session.delete(provider)
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
    """手动添加一个模型（同步上游之外的补充途径）。"""
    cleaned = model_id.strip()
    async with async_session_maker() as session:
        if await session.get(ModelProvider, provider_id) is None:
            raise ProviderNotFoundError(str(provider_id))
        duplicate = await session.scalar(
            select(ProviderModel.id).where(
                ProviderModel.provider_id == provider_id,
                ProviderModel.model_id == cleaned,
            )
        )
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
        await session.commit()
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


def _is_internal_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """环回 / 私有 / 链路本地 / 未指定 / 保留段：都不该作为出站目标。

    `is_global` 正好是这些类别的并集（还覆盖 100.64/10 这类运营商内网段），比逐个写
    `is_private` / `is_loopback` / `is_link_local` 更难漏掉一种。
    """
    return not address.is_global


async def _reject_private_target(base_url: str) -> None:
    """出站前拒绝指向内网的目标地址，抛 `ProviderFetchError`。

    后台配置的提供商共用这一条出站路径（「测试连接」与「同步模型」），而请求里带着
    库里存的明文 API Key：允许指向 127.0.0.1 / 10.x / 169.254.169.254 就等于把凭据
    交给内网任意一台机器（盲 SSRF）。

    主机名交给 `getaddrinfo` 解析而不是只认字面量：十进制写法（`http://2130706433/`）
    在解析层等价于 127.0.0.1，只看字面量会漏。解析是阻塞调用，放进线程执行。
    解析失败（域名不存在等）不是内网信号，这里不拒绝——交给后面的连接错误去报。
    """
    host = urlsplit(base_url).hostname
    if not host:
        # 缺主机名/畸形地址：让 httpx 报它自己的错，不在这里编一个网络错误
        return

    try:
        candidates = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
        except OSError:
            return
        candidates = []
        for info in infos:
            # 带 scope id 的 IPv6（fe80::1%en0）不是 ip_address 能直接吃的写法
            literal = str(info[4][0]).split("%", 1)[0]
            try:
                candidates.append(ipaddress.ip_address(literal))
            except ValueError:
                continue

    for address in candidates:
        if _is_internal_address(address):
            raise ProviderFetchError(f"Base URL 不允许指向内网地址（{address}）")


async def _fetch_remote_models(provider: ModelProvider) -> list[ModelItem]:
    """向单个提供商请求模型列表并解析。失败抛 `ProviderFetchError`。

    错误文案只给管理员看，因此不包含上游响应体（可能含有内部信息），只保留状态码
    与异常类型；完整异常写日志。
    """
    # 先确认目标不是内网，再组装 Authorization 头——这个请求带着明文 API Key
    await _reject_private_target(provider.base_url)

    headers: dict[str, str] = {}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"

    try:
        response = await get_http_client().get(
            _build_models_url(provider.base_url),
            headers=headers,
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

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ProviderFetchError("提供商响应格式不是 OpenAI /v1/models 结构")
    if not payload["data"]:
        return []

    parsed = _parse_models(payload, "", settings.model_description_field, mark_default=False)
    if parsed is None:
        return []

    # 列宽是 200：SQLite 不强制，Postgres 会直接报错；超长 id 也无法放进 ChatRequest
    # 的 300 字符上限（slug + '/' + id）。跳过并留日志，不静默截断一个真实模型名。
    too_long = [item.id for item in parsed.models if len(item.id) > 200]
    if too_long:
        logger.warning("跳过上游返回的超长模型 id: %s", too_long[:5])
    return [item for item in parsed.models if len(item.id) <= 200]


async def test_provider_connection(provider_id: uuid.UUID) -> int:
    """测试提供商可达性，返回上游声明的模型数量。"""
    provider = await get_provider(provider_id)
    if provider is None:
        raise ProviderNotFoundError(str(provider_id))
    return len(await _fetch_remote_models(provider))


async def sync_provider_models(provider_id: uuid.UUID) -> tuple[int, int, int]:
    """把上游模型列表同步进库，返回 `(上游数量, 新增, 更新)`。

    已存在模型的 `enabled` / `is_default` 保持不变；展示名与能力元数据会被上游刷新。
    上游不再返回的模型**不删除**：那可能只是临时不完整，管理员可以手动停用。
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
        created = 0
        updated = 0

        for item in remote:
            row = existing.get(item.id)
            if row is None:
                session.add(
                    ProviderModel(
                        provider_id=provider_id,
                        model_id=item.id,
                        display_name=(item.name or _default_display_name(item.id))[:200],
                        description=item.description,
                        reasoning=item.reasoning,
                        supported_reasoning_levels=list(item.supported_reasoning_levels),
                        context_window=item.context_window,
                        max_output_tokens=item.max_output_tokens,
                        enabled=True,
                    )
                )
                created += 1
                continue

            row.display_name = (item.name or _default_display_name(item.id))[:200]
            row.description = item.description
            row.reasoning = item.reasoning
            row.supported_reasoning_levels = list(item.supported_reasoning_levels)
            row.context_window = item.context_window
            row.max_output_tokens = item.max_output_tokens
            updated += 1

        await session.commit()

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


async def get_default_model_key() -> str | None:
    """当前默认模型的路由键。

    - 数据库没有提供商配置：返回 `MODEL_NAME`（旧版网关路径）；
    - 有配置但没有启用任何模型：返回 None，调用方应明确拒绝而不是偷偷回退到环境变量。
    """
    async with async_session_maker() as session:
        if not await _provider_config_exists(session):
            return settings.model_name

        rows = await _enabled_rows(session)

    if not rows:
        return None

    for model, provider in rows:
        if model.is_default:
            return make_model_key(provider.slug, model.model_id)

    model, provider = rows[0]
    return make_model_key(provider.slug, model.model_id)


async def resolve_model_target(key: str) -> ModelTarget | None:
    """把对外路由键解析成一次调用的完整目标；不存在/已停用时返回 None。

    备用模型只在**同一个提供商**内查找：`FallbackModel` 会让备用模型接受与主模型
    相同的调用参数，跨提供商的参数兼容性无法保证（例如思考等级词表不同）。
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

    target = _build_target(*match)
    fallback_name = (settings.fallback_model_name or "").strip()
    if not fallback_name or fallback_name == target.upstream_model:
        return target

    for model, provider in rows:
        if provider.id == match[1].id and model.model_id == fallback_name:
            return replace(target, fallback=_build_target(model, provider))

    return target


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


async def fetch_available_models(*, use_cache: bool = True) -> ModelListResponse:
    """获取可用模型列表。

    数据库配置已经接管时只看后台开关（不看旧网关、也不缓存）；否则才走旧版网关。
    """
    if await provider_config_exists():
        return await _public_models()

    result, _ = await _load_models(use_cache=use_cache)
    return result


async def get_known_model_ids() -> set[str] | None:
    """可用模型的路由键集合；无法校验时返回 None。

    数据库路径不会返回 None：即便一个模型都没启用，也是一个确定的空集合，调用方
    必须拒绝所有显式模型（而不是把「后台全停了」当成「校验服务不可用」放行）。
    旧版网关路径保留原语义：上游故障/兜底时返回 None。
    """
    if await provider_config_exists():
        async with async_session_maker() as session:
            rows = await _enabled_rows(session)
        return {make_model_key(provider.slug, model.model_id) for model, provider in rows}

    result, is_fallback = await _load_models(use_cache=True)
    if is_fallback:
        return None
    return {model.id for model in result.models}


async def get_model_limits(model_id: str) -> tuple[int | None, int | None]:
    """取某个模型的 `(context_window, max_output_tokens)`。

    两处返回 None 的语义都是「无法判定」，调用方（上下文预算）据此放行而不是猜一个
    默认窗口：

    1. 旧版网关列表来自兜底（上游不可用）——兜底条目刻意不伪造窗口；
    2. 这个 id 不在可用模型里——白名单已经放行过未知 id，这里不该反悔。

    单个字段缺失时只返回 `None` 的那一项（例如只报了窗口、没报最大输出）。
    数据库路径按**启用状态**查找：管理员停用模型后它也不该再参与预算判定。
    """
    if await provider_config_exists():
        async with async_session_maker() as session:
            rows = await _enabled_rows(session)
        for model, provider in rows:
            if make_model_key(provider.slug, model.model_id) == model_id:
                return model.context_window, model.max_output_tokens
        return None, None

    result, is_fallback = await _load_models(use_cache=True)
    if is_fallback:
        return None, None

    for model in result.models:
        if model.id == model_id:
            return model.context_window, model.max_output_tokens
    return None, None


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
