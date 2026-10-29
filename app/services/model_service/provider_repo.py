"""提供商/模型的行级读写：会话由调用方传入，这里不读 settings、不发 HTTP、也不提交事务。

出站校验、错误翻译与 `commit` 都留在 `provider_sync` 的用例里：仓储换个调用方（脚本、评测）
不需要伪造配置，也不需要知道管理端把哪个异常映射成 400/409。
"""

import re
import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import AppSetting, ModelProvider, ProviderModel

#: 初始化标记，写入后永不清除：删光提供商仍维持「数据库是权威」。
REGISTRY_MARKER_KEY = "model_registry_initialized"


def slugify_provider_name(name: str) -> str:
    """把提供商名称转成路由键前缀；非 ASCII 名称退回 `p<随机>`，保证非空唯一。

    slug 只生成一次，管理员改名不会改它。
    """
    base = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not base:
        return f"p{uuid.uuid4().hex[:8]}"
    return base[:48]


async def registry_configured(session: AsyncSession) -> bool:
    """数据库是否已经接管模型清单：有 registry 标记或有提供商行都算（兼容标记上线前的库）。

    只有全新库且从未配置才返回 False，此时退回旧版环境变量网关。
    """
    if await session.get(AppSetting, REGISTRY_MARKER_KEY) is not None:
        return True
    return await session.scalar(select(ModelProvider.id).limit(1)) is not None


async def mark_registry_initialized(session: AsyncSession) -> None:
    marker = await session.get(AppSetting, REGISTRY_MARKER_KEY)
    if marker is None:
        session.add(AppSetting(key=REGISTRY_MARKER_KEY, value="1"))
    else:
        marker.value = "1"


async def load_provider(session: AsyncSession, provider_id: uuid.UUID) -> ModelProvider | None:
    """按 id 读提供商，模型列表一起加载（后台展示与计数要用）。"""
    stmt = select(ModelProvider).where(ModelProvider.id == provider_id).options(selectinload(ModelProvider.models))
    return await session.scalar(stmt)


async def list_providers(session: AsyncSession) -> list[ModelProvider]:
    """全部提供商（含禁用的），带已加载的模型列表（用于计数与展示）。"""
    stmt = (
        select(ModelProvider)
        .options(selectinload(ModelProvider.models))
        .order_by(ModelProvider.created_at, ModelProvider.name)
    )
    return list((await session.scalars(stmt)).all())


async def provider_exists(session: AsyncSession, provider_id: uuid.UUID) -> bool:
    return await session.get(ModelProvider, provider_id) is not None


async def unique_provider_slug(session: AsyncSession, name: str) -> str:
    """在现有 slug 里找一个不冲突的：名称可能被改名重复使用，撞唯一约束就只能报错。"""
    base = slugify_provider_name(name)
    candidate = base
    index = 2
    while await session.scalar(select(ModelProvider.id).where(ModelProvider.slug == candidate)) is not None:
        candidate = f"{base}-{index}"[:60]
        index += 1
    return candidate


async def enabled_model_rows(session: AsyncSession) -> list[tuple[ProviderModel, ModelProvider]]:
    """所有「提供商启用 + 模型启用」的行，按后台展示顺序。"""
    stmt = (
        select(ProviderModel, ModelProvider)
        .join(ModelProvider, ProviderModel.provider_id == ModelProvider.id)
        .where(ProviderModel.enabled.is_(True), ModelProvider.enabled.is_(True))
        .order_by(ModelProvider.created_at, ProviderModel.sort_order, ProviderModel.display_name)
    )
    rows = (await session.execute(stmt)).all()
    return [(row[0], row[1]) for row in rows]


async def insert_provider(
    session: AsyncSession,
    *,
    name: str,
    slug: str,
    base_url: str,
    api_key: str | None,
    auth_style: str,
    extra_headers: dict[str, str],
    preset_key: str | None,
    enabled: bool = True,
) -> ModelProvider:
    """插入一行提供商并 flush：调用方要拿 `id` 去建模型与重新读取。"""
    provider = ModelProvider(
        name=name,
        slug=slug,
        base_url=base_url,
        api_key=api_key,
        auth_style=auth_style,
        extra_headers=extra_headers,
        preset_key=preset_key,
        enabled=enabled,
    )
    session.add(provider)
    await session.flush()
    return provider


async def delete_provider(session: AsyncSession, provider_id: uuid.UUID) -> bool:
    """删除提供商及其全部模型，返回是否真的存在。"""
    provider = await session.get(ModelProvider, provider_id)
    if provider is None:
        return False
    # 显式删子表：不把数据完整性压在外键级联上
    await session.execute(delete(ProviderModel).where(ProviderModel.provider_id == provider_id))
    await session.delete(provider)
    return True


async def list_provider_models(session: AsyncSession, provider_id: uuid.UUID) -> list[ProviderModel]:
    stmt = (
        select(ProviderModel)
        .where(ProviderModel.provider_id == provider_id)
        .order_by(ProviderModel.sort_order, ProviderModel.display_name)
    )
    return list((await session.scalars(stmt)).all())


async def find_duplicate_model_id(session: AsyncSession, provider_id: uuid.UUID, model_id: str) -> uuid.UUID | None:
    """预检同一提供商下是否已有同名模型。

    单列成函数是为了让测试能把「先查」替换成查不到：并发写入时唯一约束才是裁决者，那条路径要能单独验证。
    """
    return await session.scalar(
        select(ProviderModel.id).where(
            ProviderModel.provider_id == provider_id,
            ProviderModel.model_id == model_id,
        )
    )


async def get_model(session: AsyncSession, model_uuid: uuid.UUID) -> ProviderModel | None:
    return await session.get(ProviderModel, model_uuid)


async def insert_model(
    session: AsyncSession,
    *,
    provider_id: uuid.UUID,
    model_id: str,
    display_name: str,
    description: str | None = None,
    reasoning: bool | None = None,
    supported_reasoning_levels: list[str] | None = None,
    context_window: int | None = None,
    max_output_tokens: int | None = None,
    raw: dict | None = None,
    enabled: bool = True,
    is_default: bool = False,
) -> ProviderModel:
    model = ProviderModel(
        provider_id=provider_id,
        model_id=model_id,
        display_name=display_name,
        description=description,
        reasoning=reasoning,
        supported_reasoning_levels=list(supported_reasoning_levels or []),
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        raw=raw,
        enabled=enabled,
        is_default=is_default,
    )
    session.add(model)
    return model


async def delete_model(session: AsyncSession, model_uuid: uuid.UUID) -> bool:
    model = await session.get(ProviderModel, model_uuid)
    if model is None:
        return False
    await session.delete(model)
    return True


async def clear_default_model(session: AsyncSession, *, keep_id: uuid.UUID | None = None) -> None:
    """清掉默认标记，只留 `keep_id`：全局只能有一个默认模型。"""
    stmt = update(ProviderModel).values(is_default=False)
    if keep_id is not None:
        stmt = stmt.where(ProviderModel.id != keep_id)
    await session.execute(stmt)
