"""管理后台 HTTP 接口：模型提供商 / 模型开关 / 用户封禁 / 角色管理。

## 权限边界（三档 RBAC）

整个路由挂在 `current_admin` 上：未登录/已封禁 → 401，登录但 `role < admin` → 403。
角色定义与等级见 `app/roles.py`。

只有**改别人角色**这一个动作额外要求 `superadmin`（`current_superadmin`）。分档依据是
「误操作代价」——配错提供商会让全站没模型可用，但配错角色会让某人意外拿到或失去后台
权限，且很难靠重试纠正（本项目没有邮箱验证，也没有忘记密码）。

**没有任何接口能把自己提权**：角色接口要求 `superadmin`，而把自己从 `superadmin`
降下来的动作同样被拒绝（见 `set_user_role` 的约束）——否则一次误点就能把管理后台锁死。

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
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import User, delete_user_access_tokens, get_async_session
from app.roles import Role
from app.schemas.admin import (
    ModelCreate,
    ModelRead,
    ModelUpdate,
    PriceRead,
    PriceUpdate,
    ProviderCreate,
    ProviderRead,
    ProviderSyncResult,
    ProviderTestResult,
    ProviderUpdate,
    RoleUpdate,
    UserAdminRead,
    UserPage,
    mask_api_key,
)
from app.services import model_service, pricing
from app.services.auth import apply_role, current_admin, current_superadmin, role_assignment
from app.services.model_service import (
    ApiKeyRequiredError,
    DuplicateModelError,
    InvalidModelStateError,
    ProviderFetchError,
    ProviderNotFoundError,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin"],
    dependencies=[Depends(current_admin)],
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
    try:
        provider = await model_service.create_provider(
            name=payload.name,
            base_url=payload.base_url,
            api_key=payload.api_key,
            enabled=payload.enabled,
        )
    except ProviderFetchError as exc:
        # 地址不合法是**输入问题**（400），不是上游故障（502）：管理员改一下地址就能
        # 通过，用 502 会让他以为网关挂了。与 test/sync 的 502 刻意不同。
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return _provider_read(provider)


@router.patch("/providers/{provider_id}", response_model=ProviderRead, summary="更新模型提供商")
async def update_provider(provider_id: uuid.UUID, payload: ProviderUpdate):
    fields = payload.model_dump(exclude_unset=True)
    # 显式 null 对名称/地址/启用状态没有意义（api_key 例外：null 表示清空）
    for key in ("name", "base_url", "enabled"):
        if fields.get(key) is None:
            fields.pop(key, None)

    await _require_provider(provider_id)

    # 「换地址必须重填 key」这条规则在服务层（`model_service.update_provider`）：它需要
    # 先读出库里那条提供商才知道有没有 key 可泄露，而路由层这一层只是再查一遍。
    try:
        provider = await model_service.update_provider(provider_id, **fields)
    except ApiKeyRequiredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except ProviderFetchError as exc:
        # 与 create 一致：地址不合法是输入问题（400），不是上游故障
        raise HTTPException(status_code=400, detail=str(exc)) from None
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
# 价格
# ---------------------------------------------------------------------
def _float_or_none(value: Decimal | None) -> float | None:
    """`Decimal` → float，只用于**展示**接口。"""
    return None if value is None else float(value)


def _price_read(price: pricing.Price) -> PriceRead:
    """把服务层的 `Price` 转成对外 schema。

    `Decimal` 在这里转 float：对外是展示接口，前端只会把它格式化成人读的字符串。
    **精确计算从不经过这里**（`app/services/pricing.py` 直接用 Decimal 算），所以转换
    不会引入账单误差。
    """
    return PriceRead(
        model_id=price.model_id,
        input_per_mtok=_float_or_none(price.input_per_mtok),
        output_per_mtok=_float_or_none(price.output_per_mtok),
        cached_input_per_mtok=_float_or_none(price.cached_input_per_mtok),
        currency=price.currency,
        updated_at=price.updated_at,
    )


@router.get("/prices", response_model=list[PriceRead], summary="列出已配置的模型价格")
async def list_prices():
    """列出全部已登记的价格。

    返回的只是**配置**，不是账单：没有任何历史用量在这里。价格表可能比模型清单大——
    已删除模型的历史价格会保留（`ModelPrice.model_id` 刻意不做外键，理由见 `app/db.py`）。
    """
    return [_price_read(entry) for entry in await pricing.list_prices()]


@router.put(
    "/prices/{model_key:path}",
    response_model=PriceRead,
    summary="设置模型价格",
)
async def set_price(model_key: str, payload: PriceUpdate) -> PriceRead:
    """写入某个模型的价格（不存在则创建，存在则整体覆盖）。

    路径用 `{model_key:path}` 而不是普通参数：模型键是 `{provider.slug}/{model_id}`，
    而 `model_id` 自己也常带斜杠（`openai/gpt-4o`），整条键可能形如
    `my-gateway/openai/gpt-4o`。用普通路径参数只能拿到第一段，剩下的会被当成不存在的路由。

    `model_key` 不校验「模型是否真的存在」：价格要能**提前**录入还没启用的模型，也要能
    在模型被删后保留历史账目。这一点与 `ModelPrice` 不做外键是同一个决定。
    """
    cleaned = model_key.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="模型键不能为空")

    try:
        price = await pricing.upsert_price(
            cleaned,
            input_per_mtok=payload.input_per_mtok,
            output_per_mtok=payload.output_per_mtok,
            cached_input_per_mtok=payload.cached_input_per_mtok,
            currency=payload.currency,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return _price_read(price)


@router.delete(
    "/prices/{model_key:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除模型价格",
)
async def delete_price(model_key: str) -> Response:
    """删除价格配置；不存在时 404。

    删除后该模型的用量事件不再带金额（前端显示「未配置价格」），**不是**变成 0。
    """
    if not await pricing.delete_price(model_key.strip()):
        raise HTTPException(status_code=404, detail="价格配置不存在")
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
        role=user.role,
    )


def escape_like_pattern(term: str) -> str:
    """转义 SQL `LIKE` 的元字符，让用户输入按字面匹配。

    不转义时管理员搜一个 `%` 会列出**全部**账号（看起来像搜索坏了），搜 `a_b` 还会
    命中 `axb`——邮箱里出现下划线并不罕见。反斜杠作转义符，调用处必须同时传
    `escape="\\"`，否则 SQLite 不认这个转义（反斜杠会变成普通字符）。
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/users", response_model=UserPage, summary="搜索/筛选/分页列出用户")
async def list_users(
    query: str | None = Query(None, max_length=200, description="按邮箱模糊搜索"),
    status_filter: Literal["all", "active", "banned"] = Query(
        "all",
        alias="status",
        description="按账号状态筛选：all=全部，active=正常，banned=已封禁",
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_async_session),  # noqa: B008
):
    """列出用户。

    `status` 是为「查一下有哪些账号被封了」准备的：此前只能翻页找人，账号一多就等于没有
    这个能力。筛选必须发生在**计数之前**（`total` 要是筛选后的总数），否则分页器会指向
    一个不存在的页数。
    """
    stmt = select(User)
    cleaned = (query or "").strip()
    if cleaned:
        # 用户输入按字面匹配：`%` / `_` 是 LIKE 的元字符，不转义时搜「%」会列出全部账号
        pattern = f"%{escape_like_pattern(cleaned.lower())}%"
        stmt = stmt.where(func.lower(User.email).like(pattern, escape="\\"))
    if status_filter == "active":
        stmt = stmt.where(User.is_active.is_(True))
    elif status_filter == "banned":
        stmt = stmt.where(User.is_active.is_(False))

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
    admin: User = Depends(current_admin),  # noqa: B008
    session: AsyncSession = Depends(get_async_session),  # noqa: B008
):
    """封禁用户并立即踢下线。

    约束：不能封禁自己，也不能封禁**同级或更高级**的账号。

    这里的「级别」按 `role` 判而不是按 `is_superuser`：一个 `admin` 封不了一个
    `superadmin`，否则下级就能靠封号把上级挤掉；同一级别之间也互封不了，因为那会让
    管理后台失去可预期性（谁先动手谁赢）。

    需要收回某人的管理员身份时用角色接口降级（见 `set_user_role`），不是在这里封号。
    """
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="不能封禁当前登录的管理员账号")

    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target.role_level >= admin.role_level:
        raise HTTPException(
            status_code=400,
            detail=("不能封禁超级管理员" if target.role_level >= Role.SUPERADMIN else "不能封禁同级管理员"),
        )

    target.is_active = False
    await session.commit()
    removed = await delete_user_access_tokens(target.id)
    # 封号是「立刻失效」语义：认证依赖会拒绝他，机器也必须一起失效——否则被改过的
    # 客户端仍能拿着已存在的租约继续用算力。失败只告警：封禁本身已经生效，
    # 剩下的机器最迟会被空闲回收带走。
    try:
        from app.services.sandbox.reaper import destroy_leases_for_user

        destroyed = await destroy_leases_for_user(target.id)
        if destroyed:
            logger.info("封禁用户: id=%s 已回收执行环境=%d", target.id, destroyed)
    except Exception:  # noqa: BLE001 - 不影响封禁结果
        logger.warning("封禁用户后回收沙箱失败: id=%s", target.id, exc_info=True)
    logger.info("封禁用户: id=%s 已失效会话=%d", target.id, removed)
    return _user_read(target)


@router.post("/users/{user_id}/unban", response_model=UserAdminRead, summary="解封用户")
async def unban_user(
    user_id: uuid.UUID,
    admin: User = Depends(current_admin),  # noqa: B008
    session: AsyncSession = Depends(get_async_session),  # noqa: B008
):
    """解封用户。

    等级校验与 `ban_user` **完全对称**（不能解封同级或更高级）。不对称的后果比它
    看起来严重：超管封了一个 admin，随后**任意另一个 admin** 都能把他放回来——
    `ban_user` 想建立的「同级互封不行、收回管理员身份只能走降级」就不成立了。

    解封不删会话（对方本来就被封着，没有有效会话），所以不需要 `delete_user_access_tokens`。
    """
    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target.role_level >= admin.role_level:
        raise HTTPException(
            status_code=400,
            detail=("不能解封超级管理员" if target.role_level >= Role.SUPERADMIN else "不能解封同级管理员"),
        )

    target.is_active = True
    await session.commit()
    return _user_read(target)


# ---------------------------------------------------------------------
# 角色管理（仅 superadmin）
# ---------------------------------------------------------------------
@router.patch(
    "/users/{user_id}/role",
    response_model=UserAdminRead,
    summary="设置用户角色（仅超级管理员）",
)
async def set_user_role(
    user_id: uuid.UUID,
    payload: RoleUpdate,
    admin: User = Depends(current_superadmin),  # noqa: B008
    session: AsyncSession = Depends(get_async_session),  # noqa: B008
):
    """把一个用户的角色改成 `payload.role`。

    三条约束，每条都对应一种「一次误操作就把后台锁死」的真实故障：

    1. **不能改自己的角色**——把自己从 superadmin 降成 admin，之后连撤销都做不到。
    2. **不能修改已封禁账号的角色**——先解封再改，否则两件事的先后顺序会让「为什
       么他还是进不去」变得难以解释。
    3. **不能降级最后一个 superadmin**。

    第 3 条在单请求顺序执行时是**不可达的**：操作者自己就是超管，而第 1 条已经禁了
    改自己，所以目标也是超管就意味着库里至少有两人。保留它是因为并发场景：A 降 B 的
    同时 B 降 A，若先 `count_superadmins()` 再写，两个请求各自读到的计数都是 2，于是
    两个都通过，结果库里零个超管、后台永久锁死。

    这里用**一条带子查询的 UPDATE** 消除窗口：`WHERE role = 'superadmin' AND
    (SELECT COUNT(*) FROM user WHERE role = 'superadmin') > 1` 与写入在同一条语句里，
    SQLite 串行化写，后执行的那条会看到先提交的结果、rowcount=0，于是返回 400。

    成功后会**删掉该用户的全部会话**：角色变了但旧 token 还能用，等于权限变更要等到
    对方重新登录才生效，而那时谁也不会记得去验一下。删完对方下一次请求就是 401，
    重新登录后 `/api/auth/me` 返回新角色，前端据此显示或隐藏入口。他重新登录即可，
    密码没变，不会被锁在外面。
    """
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="不能修改自己的角色")

    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if not target.is_active:
        raise HTTPException(status_code=400, detail="该账号已被封禁，请先解封再修改角色")

    new_role = Role.parse(payload.role)
    old_role = target.role_level
    if new_role == old_role:
        # 幂等：重复提交同一个角色不算错误，但也不必写库与踢会话
        return _user_read(target)

    if old_role >= Role.SUPERADMIN > new_role:
        # 原子守卫：计数与写入必须在同一条 UPDATE 里，不能先读后写（见 docstring 第 3 条）。
        remaining_superadmins = (
            select(func.count()).select_from(User).where(User.role == Role.SUPERADMIN.value_str).scalar_subquery()
        )
        statement = (
            update(User)
            .where(
                User.id == user_id,
                User.role == Role.SUPERADMIN.value_str,
                remaining_superadmins > 1,
            )
            .values(**role_assignment(new_role))
            .execution_options(synchronize_session=False)
        )
        result = await session.execute(statement)
        if result.rowcount == 0:
            await session.rollback()
            raise HTTPException(status_code=400, detail="不能降级最后一个超级管理员，后台将无人可管")
    else:
        apply_role(target, new_role)
    await session.commit()
    # commit 后对象已过期，刷一次再序列化；不重新查库是因为同一 session 里就是刚写的行
    await session.refresh(target)

    # 权限变更必须立即生效：删掉旧会话，对方下次请求就是 401，重新登录后拿到新角色
    removed = await delete_user_access_tokens(target.id)
    logger.info(
        "角色变更: id=%s %s -> %s by=%s 已失效会话=%d",
        target.id,
        old_role.name.lower(),
        new_role.name.lower(),
        admin.id,
        removed,
    )
    return _user_read(target)
