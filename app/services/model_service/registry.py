"""管理后台维护的模型注册表：提供商 / 模型 CRUD、同步、以及对话请求的模型解析。

## 数据库是唯一权威（一旦有配置）

管理后台把「提供商 -> 模型 -> 是否启用」存进数据库（`model_provider` /
`provider_model`）。一旦库里存在**任意**提供商，数据库就是唯一权威：

- `fetch_available_models()` 只返回已启用提供商下的已启用模型；
- `get_known_model_ids()` 返回这些模型的路由键（空集合就是「一个都没有」，
  而不是「无法校验」）；
- 禁用一个模型会立刻从列表消失，对话请求也会被白名单拒绝。

库里一条提供商都没有时（旧部署、测试、尚未引导）才退回原有的「直接读
`LITELLM_API_BASE` 的 `/v1/models`」路径（见 `gateway`）。这条兜底只为了升级不中断，
不是第二套配置入口：首次启动会用环境变量**引导**出一条「默认网关」提供商
（`seed_default_provider`），之后由后台接管。

## 路由键

对外模型 id 是 `{provider.slug}/{model_id}`。不同提供商可以各自提供 `gpt-4o`，
只用 `model_id` 做键会互相覆盖；`slug` 生成后不随改名变化，用户已保存的选择不会
因为管理员改个名字就指向另一个模型。

## 为什么数据库路径不缓存

模型启停是管理员刚点完就要生效的操作。管理后台的写操作与对话请求不在同一个进程
生命周期里没有通知机制，任何 TTL 缓存都会造成「明明禁用了还能用」。两张表都很小，
每次请求多两条查询比缓存失效逻辑便宜得多。旧版网关路径的缓存保持原样（在 `gateway`）。

## 一次查询解析一轮对话

`resolve_chat_model` 把「白名单校验 + 目标解析 + 窗口/输出上限」合并成**一次**读库：
此前 `/api/chat` 一次请求要重复全量查一遍启用模型四次（API 层白名单、服务层解析、
`token_budget` 的预算、流尾的用量窗口）。快照把前三者的数据一次取齐，`token_budget`
内部那次查询按约定保留（它不归本模块管），于是每请求只剩两次。
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
from app.services.model_target import ModelTarget

logger = logging.getLogger(__name__)

#: 管理后台的「测试连接 / 同步模型」是显式动作，可以等久一点；但也不能无限挂住。
PROVIDER_SYNC_TIMEOUT_SECONDS = 15.0

DEFAULT_PROVIDER_NAME = "默认网关"

#: 模型注册表初始化标记。写入后永不清除：删光提供商仍然维持「数据库是权威」。
REGISTRY_MARKER_KEY = "model_registry_initialized"

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


async def _build_target(model: ProviderModel, provider: ModelProvider) -> ModelTarget:
    """组装一次调用的目标，并在**所有出站之前**校验地址。

    为什么把守卫放在这里：这是唯一构造带 `base_url` 的 `ModelTarget` 的地方
    （主模型与备用模型都走它），也就卡住了 `build_provider` 的出站。放在 CRUD 层
    不够——库里可能有守卫上线前就存在的行，或者写入时严格模式还是关的。

    校验失败会抛 `ProviderFetchError` 而不是返回 None：`None` 的语义是「模型不存在」，
    会变成用户侧的「模型已停用或不存在，请刷新模型列表」——而真实原因是服务端配置
    违规，让用户去刷一个本来正常的列表只会误导。错误分类见
    `app/services/chat_service.py` 的预检兜底（会归为 internal_error 并附关联 ID）。

    代价：每个对话请求多一次地址判断。字面量 IP（自建 vLLM、`127.0.0.1`）是纯计算；
    域名要走 `getaddrinfo`（已放进线程），相对上游几秒的等待可以忽略。这里**不缓存**
    结果：缓存会把「先解析公网、后重绑内网」的窗口从单次请求拉到整个进程生命周期，
    等于用一点延迟换掉 DNS rebinding 的防线。
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
    """备用模型名（口径见 `app/services/fallback_config.py`）。

    这里只管「配没配」，能不能真的用上由调用方按库里的启用状态判断；配了却用不上时
    调用方要 `warn_fallback_unavailable`，否则运维以为自己有兜底。
    """
    return configured_fallback_name(primary_name)


@dataclass(frozen=True, slots=True)
class ChatModelSnapshot:
    """一轮对话的模型解析结果：路由键、调用目标、窗口/输出上限、白名单。

    一次读库取齐这些数据，让 API 层、服务层与流尾的用量事件共用同一份取值。
    `context_window` / `max_output_tokens` 与 `fallback_*` 分开存而不是查库：备用模型
    接手时用量事件必须报**真正回答的那个模型**的窗口，否则两个模型的窗口会混在一起。

    `known_ids is None` 表示无法校验（旧版网关上游故障/兜底），此时白名单**放行**；
    `key` 为空串表示「数据库已接管但一个模型都没启用」，调用方必须明确拒绝而不是
    退回环境变量。
    """

    key: str
    target: ModelTarget | None
    context_window: int | None
    max_output_tokens: int | None
    fallback_key: str | None
    fallback_context_window: int | None
    known_ids: frozenset[str] | None
    registry_managed: bool

    def context_window_for(self, answered_by: str) -> int | None:
        """本轮实际回答的模型对应的上下文窗口。"""
        if self.fallback_key is not None and answered_by == self.fallback_key:
            return self.fallback_context_window
        return self.context_window


async def resolve_chat_model(requested_key: str | None) -> ChatModelSnapshot:
    """解析本轮对话使用的模型：白名单 + 目标 + 窗口/输出上限，一次读库。

    `requested_key` 为 None 时取后台默认模型。数据库路径下按启用状态查找：未知或已
    停用的模型解析结果为 `target=None`，调用方必须明确拒绝（不能顺手退回环境变量里的
    旧网关，用户会以为自己在用一个已被停用的模型）。

    备用模型只在**同一个提供商**内查找：`FallbackModel` 会让备用模型接受与主模型
    相同的调用参数，跨提供商的参数兼容性无法保证（例如思考等级词表不同）。
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
    )


async def _resolve_from_gateway(requested_key: str | None) -> ChatModelSnapshot:
    """旧版网关路径的解析：列表来自缓存，不碰数据库。

    这里没有 per-provider 的 `ModelTarget`（凭据与地址来自环境变量，由
    `agent_factory.build_provider` 兜底），上限从网关列表里按 id 取。

    白名单保持原语义：网关列表不可用时 `known_ids=None`（放行），而不是空集合
    （那会把「上游故障」判成「所有模型都不可用」）。
    """
    result, is_fallback = await gateway._load_models(use_cache=True)
    key = requested_key or settings.model_name
    known_ids = None if is_fallback else frozenset(model.id for model in result.models)

    def limits_of(model_id: str) -> tuple[int | None, int | None]:
        if is_fallback:
            # 兜底条目刻意不伪造窗口
            return None, None
        for model in result.models:
            if model.id == model_id:
                return model.context_window, model.max_output_tokens
        return None, None

    context_window, max_output_tokens = limits_of(key)
    fallback_key = _configured_fallback_name(key)
    fallback_context_window = limits_of(fallback_key)[0] if fallback_key is not None else None

    return ChatModelSnapshot(
        key=key,
        target=None,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        fallback_key=fallback_key,
        fallback_context_window=fallback_context_window,
        known_ids=known_ids,
        registry_managed=False,
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


#: 引导用的网关地址必须是 http(s)：写错成 `localhost:4000`（少了 scheme）或整段黏在
#: 别的变量里时，httpx 会在第一次请求前抛 `UnsupportedProtocol`，而**用户看到的**
#: 是「模型不可用」——配置错误的现场证据只剩一行启动日志。
_BOOTSTRAP_ALLOWED_SCHEMES = frozenset({"http", "https"})


def validate_bootstrap_base_url(raw: str | None) -> tuple[str | None, str]:
    """校验 `LITELLM_API_BASE` 能否用来引导提供商；返回 `(地址, 不可用原因)`。

    只做**不需要联网**的检查（scheme / 主机名 / 元数据族字面量 IP）：出站地址守卫
    （`outbound_guard._reject_private_target`）会在真正发请求时按当时解析结果再拦一次，
    启动路径上不该为了校验去跑一次 DNS。

    为什么要在**写库之前**拦：引导一旦写进库，`_provider_config_exists` 就为真、后台
    成为权威，环境变量路径彻底停用。一条写错的 `LITELLM_API_BASE` 于是把刚部署好的
    实例变成「零个可用模型」，只能进管理后台修——而这本来是一行配置笔误。拦住时保持
    「后台未接管」，服务继续按环境变量工作。
    """
    # 先按原值校验再归一化：`http://` 去掉尾斜杠会变成 `http:`，报错信息里就成了
    # 一个没人写过的值，反而看不出问题。
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
    """首次启动时把环境变量里的网关引导成一条提供商记录。

    只写库、不访问上游：启动路径上不应该因为网关不可达而变慢或失败。管理员进入
    后台点一次「同步模型」即可补齐列表；在那之前至少有一个可用的默认模型。

    返回是否真的写入了记录（便于启动日志与测试断言）。
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
    base_url: str,
    api_key: str | None = None,
    enabled: bool = True,
) -> ModelProvider:
    """新建提供商。slug 由名称生成并保证唯一。

    地址在这里就校验（而非等到第一次「测试连接」）：管理员填错内网/元数据地址时
    应当当场看到 400 与原因，而不是存进去之后某次对话才静默失败。

    并发建同名提供商时 `_unique_provider_slug` 的「先查后插」会双双落空，第二个请求
    撞 `slug` 唯一约束。裸 `IntegrityError` 是 500，管理员只会看到「服务器错误」；
    这里翻译成可读的重试提示（路由层映射为 400）。
    """
    await outbound_guard._reject_private_target(base_url.strip())
    async with async_session_maker() as session:
        provider = ModelProvider(
            name=name.strip(),
            slug=await _unique_provider_slug(session, name),
            base_url=base_url.strip().rstrip("/"),
            api_key=(api_key or None),
            enabled=enabled,
        )
        session.add(provider)
        try:
            # 首次创建提供商即视为「后台已经接管」：之后即使删光提供商，也不回退到环境变量。
            # 它必须和 `commit` 在同一个 try 里：`_mark_registry_initialized` 里的
            # `session.get` 会触发 autoflush，slug 撞唯一约束**就在那一刻**抛出来，
            # 而不是等到 commit。
            await _mark_registry_initialized(session)
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            logger.warning("创建提供商撞唯一约束（可能是并发创建同名提供商）: name=%s", name)
            raise ProviderFetchError("提供商名称与另一个正在创建的提供商冲突，请重试") from exc

        loaded = await _load_provider(session, provider.id)
        if loaded is None:
            # 理论上不可能（刚提交完），但用 `assert` 的话 `python -O` 会把它删掉，
            # 返回 None 而类型标注仍写着 ModelProvider，错误点被推到调用方。
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

    换地址等于换凭据：新地址的主人能在下次「测试连接」里收到库里那份明文 key，
    所以**有 key 的**提供商改地址时必须同时重新填写 key。只改名字/启用状态时仍然
    可以不填（前端每次保存都会原样回传 base_url，因此这里比的是**是否变化**，不是
    「有没有传」）。没有 key 的提供商本来就没有可泄露的凭据，不拦——此前那条规则
    不看有没有 key，导致明明配的是无凭据内网网关却改不动地址。
    """
    async with async_session_maker() as session:
        provider = await _load_provider(session, provider_id)
        if provider is None:
            return None

        if name is not _UNSET:
            provider.name = str(name).strip()
        if base_url is not _UNSET:
            # 只校验变化过的新地址：不动地址的保存不该因为旧行违规而失败（那种行由
            # `_build_target` 在真正出站时拦住）。
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

        # 任何后台写操作都算「后台已接管」：只靠 create / seed 写标记的话，
        # 标记功能上线前就已配置的库（只有 provider 行、没有 marker）在删光提供商后
        # 重启会被环境变量重新引导出一条「默认网关」，与 settings 的承诺相反。
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

    同样写入 registry 标记：删光提供商是**管理员的明确意图**，不代表「回到出厂状态」。
    不写标记的话，旧库（只有 provider 行、没有 marker）删完重启会被
    `seed_default_provider` 用 `LITELLM_API_BASE` 重建一条默认网关，用户视角是
    「我删干净了，重启后模型又回来了」。

    需要暂时停用某个提供商请用 `enabled=False`，而不是删除——这一点与
    `settings.bootstrap_provider_from_env` 的说明一致。
    """
    async with async_session_maker() as session:
        provider = await session.get(ModelProvider, provider_id)
        if provider is None:
            return False
        # 显式删子表：SQLite 的 FK 级联依赖连接级 PRAGMA，这里不把数据完整性压在它上面
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

    单列成函数是为了让「先查」这一步可以被测试替换成「查不到」——并发写入时它必然
    查不到（另一边的插入还没提交），唯一约束才是真正的裁决者，那条路径必须能单独验证。
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

    并发添加同一个 `model_id` 时「先查后插」同样会双双落空，第二个请求撞
    `uq_provider_model`。翻译成 `DuplicateModelError`（路由层已映射为 409），
    而不是让裸 `IntegrityError` 变成 500。
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


async def _fetch_remote_models(provider: ModelProvider) -> list[ModelItem]:
    """向单个提供商请求模型列表并解析。失败抛 `ProviderFetchError`。

    错误文案只给管理员看，因此不包含上游响应体（可能含有内部信息），只保留状态码
    与异常类型；完整异常写日志。
    """
    # 先检查目标地址，再组装 Authorization 头——这个请求带着明文 API Key
    await outbound_guard._reject_private_target(provider.base_url)

    headers: dict[str, str] = {}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"

    try:
        response = await get_http_client().get(
            gateway._build_models_url(provider.base_url),
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

    parsed = gateway._parse_models(payload, "", settings.model_description_field, mark_default=False)
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

    ## 两个曾经会变成 500 的输入

    1. **上游响应里同一个 id 出现两次**（网关聚合多个上游时常见）。之前的实现
       每次 `existing.get(item.id)` 落空就 `session.add`，第二条重复行提交时撞
       `uq_provider_model`（`app/db.py`）→ `IntegrityError` → 未捕获 → 500，
       而且整次同步（包括本来有效的新增）随事务回滚一起丢弃。
    2. **两个管理员并发点「同步」**。两个 session 各自读到空快照，各自插入同一个
       `(provider_id, model_id)`，同样撞约束。这种竞态没法靠「读之前再查一次」解决，
       只能把唯一约束当作权威、把冲突翻译成可读的重试提示。

    所以循环里维护快照（新建的行也塞回 `existing`，重复 id 直接跳过），并在提交处
    捕获 `IntegrityError`。
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
        #: 本轮已经处理过的 id。上游重复返回同一个 id 时，第二次直接跳过：
        #: 既不能重复 add（撞唯一约束），也不该把它当成「更新」（两次内容一样，
        #: 报成更新会让「新增/更新」计数失真）。
        seen: set[str] = set()
        created = 0
        updated = 0
        duplicated = 0

        for item in remote:
            if item.id in seen:
                duplicated += 1
                continue
            seen.add(item.id)

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

        if duplicated:
            logger.warning("上游模型列表里有 %d 个重复 id，已跳过（provider=%s）", duplicated, provider.name)

        try:
            await session.commit()
        except IntegrityError as exc:
            # 并发同步：另一个请求抢先插入了同一 (provider_id, model_id)。
            # 把约束冲突翻译成可重试的提示，而不是让 500 消失得无影无踪。
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
    """获取可用模型列表。

    数据库配置已经接管时只看后台开关（不看旧网关、也不缓存）；否则才走旧版网关。
    """
    if await provider_config_exists():
        return await _public_models()

    result, _ = await gateway._load_models(use_cache=use_cache)
    return result


async def get_default_model_key() -> str | None:
    """当前默认模型的路由键。

    - 数据库没有提供商配置：返回 `MODEL_NAME`（旧版网关路径）；
    - 有配置但没有启用任何模型：返回 None，调用方应明确拒绝而不是偷偷回退到环境变量。
    """
    async with async_session_maker() as session:
        if not await _provider_config_exists(session):
            return settings.model_name

        rows = await _enabled_rows(session)

    return _default_key_of(rows)


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

    result, is_fallback = await gateway._load_models(use_cache=True)
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

    对话路径不再逐帧调它（改用 `resolve_chat_model` 的一次查询快照，
    见 `ChatModelSnapshot`）；本函数保留给 `token_budget` 等按名字取窗口的调用方。
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
    """把对外路由键解析成一次调用的完整目标；不存在/已停用时返回 None。

    与 `resolve_chat_model` 的差别只有「不带白名单、默认键与窗口上限」，语义与拆分前
    一致：单独需要 target 的调用方（标题生成、CLI 装配）可以继续用它，但它每次都要
    自己查一遍库——一个请求里既解析目标又算预算时应该用 `resolve_chat_model`。
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
