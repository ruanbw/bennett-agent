"""管理后台 HTTP 接口：模型提供商 / 模型开关 / 用户封禁。

## 权限边界

整个路由挂在 `current_superuser` 上：未登录/已封禁 → 401，登录但不是超级管理员 →
403。超级管理员只能来自 `SUPERUSER_EMAILS` 环境变量（见 `app/services/auth.py`），
没有任何接口能把自己或别人提升为管理员——管理后台不该成为提权路径。

## 封禁的真实语义

`is_active=False` 是 fastapi-users 自己的停用位，`current_active_user` 会直接拒绝；
同时这里显式删掉该用户的**全部** `access_token` 行，让已经打开的页面/在途请求立刻
失效，而不是等 cookie 过期。解封只恢复登录能力，不恢复旧会话（旧 token 已经删了）。

## 为什么提供商删除是硬删除

模型清单完全由后台维护，没有用户数据挂在上面；保留「已删除提供商」的软状态只会让
`/api/models` 的过滤逻辑多一个分支。删错了重新添加即可，代价是重新同步一次。
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import User, delete_user_access_tokens, get_async_session
from app.schemas.admin import (
    ModelCreate,
    ModelRead,
    ModelUpdate,
    ProviderCreate,
    ProviderRead,
    ProviderSyncResult,
    ProviderTestResult,
    ProviderUpdate,
    UserAdminRead,
    UserPage,
    mask_api_key,
)
from app.services import model_service
from app.services.auth import current_superuser
from app.services.model_service import (
    DuplicateModelError,
    InvalidModelStateError,
    ProviderFetchError,
    ProviderNotFoundError,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(current_superuser)],
)


def _provider_read(provider) -> ProviderRead:  # noqa: ANN001 - ORM 对象，避免在这里引入导入环
    return ProviderRead(
        id=provider.id,
        name=provider.name,
        slug=provider.slug,
        base_url=provider.base_url,
        api_key_hint=mask_api_key(provider.api_key),
        enabled=provider.enabled,
        model_count=len(provider.models or []),
        created_at=provider.created_at,
    )


def _model_read(model, provider) -> ModelRead:  # noqa: ANN001
    return ModelRead(
        id=model.id,
        provider_id=model.provider_id,
        key=model_service.make_model_key(provider.slug, model.model_id),
        model_id=model.model_id,
        display_name=model.display_name,
        description=model.description,
        reasoning=model.reasoning,
        supported_reasoning_levels=list(model.supported_reasoning_levels or []),
        context_window=model.context_window,
        max_output_tokens=model.max_output_tokens,
        enabled=model.enabled,
        is_default=model.is_default,
    )


async def _require_provider(provider_id: uuid.UUID):  # noqa: ANN202
    provider = await model_service.get_provider(provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="提供商不存在")
    return provider


# ---------------------------------------------------------------------
# 提供商
# ---------------------------------------------------------------------
@router.get("/providers", response_model=list[ProviderRead], summary="列出模型提供商")
async def list_providers():
    providers = await model_service.list_providers()
    return [_provider_read(provider) for provider in providers]


@router.post(
    "/providers",
    response_model=ProviderRead,
    status_code=status.HTTP_201_CREATED,
    summary="新增模型提供商",
)
async def create_provider(payload: ProviderCreate):
    provider = await model_service.create_provider(
        name=payload.name,
        base_url=payload.base_url,
        api_key=payload.api_key,
        enabled=payload.enabled,
    )
    return _provider_read(provider)


@router.patch("/providers/{provider_id}", response_model=ProviderRead, summary="更新模型提供商")
async def update_provider(provider_id: uuid.UUID, payload: ProviderUpdate):
    fields = payload.model_dump(exclude_unset=True)
    # 显式 null 对名称/地址/启用状态没有意义（api_key 例外：null 表示清空）
    for key in ("name", "base_url", "enabled"):
        if fields.get(key) is None:
            fields.pop(key, None)

    provider = await _require_provider(provider_id)

    # 换地址等于换凭据：新地址的主人能在下次「测试连接」里收到库里那份明文 key，
    # 所以没重新填 key 就不许改地址。只改名字/启用状态时仍然可以不填（前端每次保存
    # 都会原样回传 base_url，因此这里比的是**是否变化**，不是「有没有传」）。
    if "base_url" in fields and fields["base_url"] != provider.base_url and "api_key" not in fields:
        raise HTTPException(status_code=400, detail="更换 Base URL 时必须重新填写 API Key")

    provider = await model_service.update_provider(provider_id, **fields)
    if provider is None:
        raise HTTPException(status_code=404, detail="提供商不存在")
    return _provider_read(provider)


@router.delete(
    "/providers/{provider_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除模型提供商",
)
async def delete_provider(provider_id: uuid.UUID) -> Response:
    if not await model_service.delete_provider(provider_id):
        raise HTTPException(status_code=404, detail="提供商不存在")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/providers/{provider_id}/test",
    response_model=ProviderTestResult,
    summary="测试提供商连通性",
)
async def test_provider(provider_id: uuid.UUID):
    try:
        count = await model_service.test_provider_connection(provider_id)
    except ProviderNotFoundError:
        raise HTTPException(status_code=404, detail="提供商不存在") from None
    except ProviderFetchError as exc:
        # 测试连接失败是**预期结果**，不是 5xx：管理界面需要把原因显示在按钮旁边
        return ProviderTestResult(ok=False, model_count=0, error=str(exc))
    return ProviderTestResult(ok=True, model_count=count)


@router.post(
    "/providers/{provider_id}/sync",
    response_model=ProviderSyncResult,
    summary="同步提供商模型列表",
)
async def sync_provider(provider_id: uuid.UUID):
    try:
        fetched, created, updated = await model_service.sync_provider_models(provider_id)
    except ProviderNotFoundError:
        raise HTTPException(status_code=404, detail="提供商不存在") from None
    except ProviderFetchError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return ProviderSyncResult(fetched=fetched, created=created, updated=updated)


# ---------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------
@router.get(
    "/providers/{provider_id}/models",
    response_model=list[ModelRead],
    summary="列出某提供商下的模型",
)
async def list_provider_models(provider_id: uuid.UUID):
    provider = await _require_provider(provider_id)
    models = await model_service.list_provider_models(provider_id)
    return [_model_read(model, provider) for model in models]


@router.post(
    "/providers/{provider_id}/models",
    response_model=ModelRead,
    status_code=status.HTTP_201_CREATED,
    summary="手动添加模型",
)
async def create_provider_model(provider_id: uuid.UUID, payload: ModelCreate):
    provider = await _require_provider(provider_id)
    try:
        model = await model_service.create_provider_model(
            provider_id,
            model_id=payload.model_id,
            display_name=payload.display_name,
            description=payload.description,
            reasoning=payload.reasoning,
            supported_reasoning_levels=payload.supported_reasoning_levels,
            context_window=payload.context_window,
            max_output_tokens=payload.max_output_tokens,
            enabled=payload.enabled,
            is_default=payload.is_default,
        )
    except DuplicateModelError:
        raise HTTPException(status_code=409, detail="该模型已存在") from None
    except InvalidModelStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return _model_read(model, provider)


@router.patch("/models/{model_uuid}", response_model=ModelRead, summary="更新模型")
async def update_model(model_uuid: uuid.UUID, payload: ModelUpdate):
    fields = payload.model_dump(exclude_unset=True)
    # 展示名不允许显式清成 null；空字符串由服务层回退成 model_id
    if fields.get("display_name") is None:
        fields.pop("display_name", None)

    try:
        model = await model_service.update_provider_model(model_uuid, **fields)
    except InvalidModelStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if model is None:
        raise HTTPException(status_code=404, detail="模型不存在")

    provider = await model_service.get_provider(model.provider_id)
    if provider is None:
        raise HTTPException(status_code=409, detail="模型所属提供商不存在")
    return _model_read(model, provider)


@router.delete(
    "/models/{model_uuid}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除模型",
)
async def delete_model(model_uuid: uuid.UUID) -> Response:
    if not await model_service.delete_provider_model(model_uuid):
        raise HTTPException(status_code=404, detail="模型不存在")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------
# 用户管理
# ---------------------------------------------------------------------
def _user_read(user: User) -> UserAdminRead:
    return UserAdminRead(
        id=user.id,
        email=user.email,
        is_active=user.is_active,
        is_superuser=user.is_superuser,
        is_verified=user.is_verified,
    )


@router.get("/users", response_model=UserPage, summary="搜索/分页列出用户")
async def list_users(
    query: str | None = Query(None, max_length=200, description="按邮箱模糊搜索"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_async_session),  # noqa: B008
):
    stmt = select(User)
    cleaned = (query or "").strip()
    if cleaned:
        stmt = stmt.where(func.lower(User.email).like(f"%{cleaned.lower()}%"))

    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    users = (await session.scalars(stmt.order_by(User.email).limit(limit).offset(offset))).all()
    return UserPage(
        items=[_user_read(user) for user in users],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/users/{user_id}/ban", response_model=UserAdminRead, summary="封禁用户")
async def ban_user(
    user_id: uuid.UUID,
    admin: User = Depends(current_superuser),  # noqa: B008
    session: AsyncSession = Depends(get_async_session),  # noqa: B008
):
    """封禁用户并立即踢下线。

    约束：不能封禁自己（唯一能进后台的人把自己锁在外面，只能改环境变量重启），
    也不能封禁其他超级管理员（同级账号互封会让管理后台失去可预期性）。需要收回
    某人的管理员身份时，应从 `SUPERUSER_EMAILS` 移除后重启，而不是在这里点封禁。
    """
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="不能封禁当前登录的管理员账号")

    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target.is_superuser:
        raise HTTPException(status_code=400, detail="不能封禁超级管理员")

    target.is_active = False
    await session.commit()
    removed = await delete_user_access_tokens(target.id)
    logger.info("封禁用户: id=%s 已失效会话=%d", target.id, removed)
    return _user_read(target)


@router.post("/users/{user_id}/unban", response_model=UserAdminRead, summary="解封用户")
async def unban_user(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_async_session),  # noqa: B008
):
    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    target.is_active = True
    await session.commit()
    return _user_read(target)
