"""管理后台的请求/响应模型。

API key 只进不出：响应里只有掩码，编辑时留空表示不变，避免它出现在浏览器缓存/日志/截图里。
`base_url` 只接受 http(s)。更新用 `None`/缺省表示「不修改」，与 `model_service` 的哨兵语义对齐。
"""

import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.roles import ALL_ROLES, ROLE_USER
from app.services.model_service.presets import AUTH_STYLES

MAX_NAME_CHARS = 80
MAX_URL_CHARS = 500
MAX_API_KEY_CHARS = 1000
MAX_MODEL_ID_CHARS = 200

#: 自定义请求头（`extra_headers`）的名字白名单：字母/数字/连字符，覆盖 Azure 的 `api-key`
#: 与私有网关的 `x-tenant-id`，同时挡掉空格、冒号这类会让 httpx 自己抛 `InvalidHeader` 的写法。
EXTRA_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")

#: 固定请求头是配置而不是数据面：超过这个数量说明用法错了，而每一项都会进每条出站请求。
MAX_EXTRA_HEADERS = 20

#: 控制字符（含 CR/LF/TAB 与控制台转义序列）。它能伪造一整行日志：这些名字会进 `provider_sync`
#: 的文本日志，而日志正是目前唯一的管理动作审计证据。
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _reject_control_chars(value: str) -> str:
    """拒绝名字/值里的控制字符（两端 strip 挡不住名字中间的 CR/LF）。"""
    if CONTROL_CHARS.search(value):
        raise ValueError("不能包含控制字符（含换行）")
    return value


def _validate_auth_style(value: str | None) -> str | None:
    """白名单来自 `presets.AUTH_STYLES`（单一真源）：拼错一个认证方式会让出站请求无凭据、上游 401。"""
    if value is None:
        return None
    if value not in AUTH_STYLES:
        raise ValueError(f"认证方式必须是 {sorted(AUTH_STYLES)} 之一")
    return value


def _validate_extra_headers(value: dict[str, str] | None) -> dict[str, str] | None:
    """自定义请求头的入口校验：名字形状、值里不得有 CR/LF、条数上限。

    值里带换行时 httpx 会抛 `LocalProtocolError`（变成 502），既不告诉管理员是哪个头，
    也让「上游挂了」与「配置错了」在日志里无法区分。
    """
    if value is None:
        return None
    if len(value) > MAX_EXTRA_HEADERS:
        raise ValueError(f"自定义请求头最多 {MAX_EXTRA_HEADERS} 项")
    for name, header_value in value.items():
        if not EXTRA_HEADER_NAME.match(name or ""):
            raise ValueError(f"请求头名不合法：{name!r}（只允许字母、数字与连字符，1-64 位）")
        _reject_control_chars(header_value or "")
    return value


def mask_api_key(api_key: str | None) -> str | None:
    """把 API key 变成可安全展示的掩码。

    长度很短时不留任何字符：`sk-1` 没有可打码的余地，露出 2 个字符等于泄露一半。
    """
    if not api_key:
        return None
    if len(api_key) <= 8:
        return "••••"
    return f"{api_key[:3]}…{api_key[-4:]}"


class ProviderRead(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    base_url: str
    api_key_hint: str | None
    enabled: bool
    model_count: int
    created_at: datetime
    #: 认证方式（API Key 放进哪个头），见 `model_service.presets.AUTH_STYLES`。
    auth_style: str = "bearer"
    #: 创建时使用的内置预设 key；自定义添加为 None。
    preset_key: str | None = None


class ProviderPresetRead(BaseModel):
    """内置厂商预设（`GET /api/admin/provider-presets`）。

    `note` 说明该厂商的模型列表能同步到哪些元数据，避免管理员对「同步完窗口还是未知」感到意外。
    """

    key: str
    name: str
    base_url: str
    auth_style: str
    api_key_url: str
    needs_api_key: bool
    note: str = ""
    #: 选择界面的分组：overseas=海外厂商 / cn=国内厂商 / local=本机网关。
    group: str = "overseas"


class ProviderCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=MAX_NAME_CHARS)
    #: 选了厂商预设时可省略（用预设地址）；自定义添加必填。
    base_url: str | None = Field(None, max_length=MAX_URL_CHARS)
    preset_key: str | None = Field(None, max_length=40)
    api_key: str | None = Field(None, max_length=MAX_API_KEY_CHARS)
    #: API Key 放进哪个头。不传时跟随预设/默认 `bearer`；Azure OpenAI 必须选 `api-key`
    #: （它不收 `Authorization: Bearer`，头名也不是 `x-api-key`）。
    auth_style: str | None = None
    #: 固定请求头：需要额外头（如 Azure 的私有网关头）的提供商靠它接入。
    extra_headers: dict[str, str] | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def _require_base_url_or_preset(self) -> "ProviderCreate":
        if not self.preset_key and not self.base_url:
            raise ValueError("自定义提供商必须填写接口地址（或选择一个厂商预设）")
        return self

    @field_validator("name", "base_url")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("不能为空白")
        return _reject_control_chars(cleaned)

    @field_validator("base_url")
    @classmethod
    def _require_http_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith(("http://", "https://")):
            raise ValueError("必须是以 http:// 或 https:// 开头的地址")
        return value.rstrip("/")

    @field_validator("api_key")
    @classmethod
    def _clean_api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("auth_style")
    @classmethod
    def _known_auth_style(cls, value: str | None) -> str | None:
        return _validate_auth_style(value)

    @field_validator("extra_headers")
    @classmethod
    def _clean_extra_headers(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return _validate_extra_headers(value)


class ProviderUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=MAX_NAME_CHARS)
    base_url: str | None = Field(None, min_length=1, max_length=MAX_URL_CHARS)
    #: None = 不修改；空串 = 清空；非空 = 更新。用 `model_fields_set` 区分「没传」与
    #: 「显式传 null」，路由层只把请求里真正出现的键往下传。
    api_key: str | None = Field(None, max_length=MAX_API_KEY_CHARS)
    auth_style: str | None = None
    #: 与 `api_key` 一样是凭据配置：传 `{}` 表示清空全部固定头，不传则保持不变。
    extra_headers: dict[str, str] | None = None
    enabled: bool | None = None

    @field_validator("name", "base_url")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("不能为空白")
        return _reject_control_chars(cleaned)

    @field_validator("auth_style")
    @classmethod
    def _known_auth_style(cls, value: str | None) -> str | None:
        return _validate_auth_style(value)

    @field_validator("extra_headers")
    @classmethod
    def _clean_extra_headers(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return _validate_extra_headers(value)

    @field_validator("base_url")
    @classmethod
    def _require_http_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.startswith(("http://", "https://")):
            raise ValueError("必须是以 http:// 或 https:// 开头的地址")
        return value.rstrip("/")


class ModelRead(BaseModel):
    id: uuid.UUID
    provider_id: uuid.UUID
    key: str
    model_id: str
    display_name: str
    description: str | None
    reasoning: bool | None
    supported_reasoning_levels: list[str]
    context_window: int | None
    max_output_tokens: int | None
    enabled: bool
    is_default: bool
    #: 最近一次同步的上游原始条目；手动添加或从未同步过为 None。
    raw: dict | None = None


class ModelCreate(BaseModel):
    model_id: str = Field(..., min_length=1, max_length=MAX_MODEL_ID_CHARS)
    display_name: str | None = Field(None, max_length=MAX_MODEL_ID_CHARS)
    description: str | None = Field(None, max_length=1000)
    reasoning: bool | None = None
    supported_reasoning_levels: list[str] = Field(default_factory=list)
    #: 下界 1024：`build_budget` 先扣约 1.5k 固定开销再乘安全系数，填 1 会让该模型下每条对话都
    #: 超限；上界 10M：列是 PG `INTEGER`，2^31 会让 asyncpg 抛 DataError 变 500，而它已远超真实模型。
    context_window: int | None = Field(None, ge=1024, le=10_000_000)
    max_output_tokens: int | None = Field(None, ge=1024, le=10_000_000)
    enabled: bool = True
    is_default: bool = False

    @field_validator("model_id")
    @classmethod
    def _strip_model_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("不能为空白")
        return cleaned


class ModelUpdate(BaseModel):
    display_name: str | None = Field(None, min_length=1, max_length=MAX_MODEL_ID_CHARS)
    description: str | None = Field(None, max_length=1000)
    reasoning: bool | None = None
    supported_reasoning_levels: list[str] | None = None
    #: 量纲约束与 `ModelCreate` 同一套理由：填 1 会让该模型下每条对话都被判超限，
    #: 填 2^31 会让 PG INTEGER 溢出成 500。
    context_window: int | None = Field(None, ge=1024, le=10_000_000)
    max_output_tokens: int | None = Field(None, ge=1024, le=10_000_000)
    enabled: bool | None = None
    is_default: bool | None = None


class ProviderSyncResult(BaseModel):
    fetched: int
    created: int
    updated: int


class ProviderTestResult(BaseModel):
    ok: bool
    model_count: int
    error: str | None = None


class UserAdminRead(BaseModel):
    id: uuid.UUID
    email: str
    is_active: bool
    is_superuser: bool
    is_verified: bool
    #: 角色字符串（`user` / `admin` / `superadmin`），前端展示角色标识与按钮禁用状态都靠它。
    role: str = ROLE_USER


class RoleUpdate(BaseModel):
    """角色变更请求。

    用 `Literal` 而不是裸 `str`：非法值必须在入口就 422，而不是被 `Role.parse` 悄悄降级成
    `user`。`superadmin` 刻意不在允许值里：最高一档只能经运维通道（`scripts/seed_admin.py`）授予。
    """

    role: Literal["user", "admin"]

    @field_validator("role")
    @classmethod
    def _known(cls, value: str) -> str:
        # Literal 已挡住非法值；显式检查让「新增中档位却忘了同步 ALL_ROLES」在这里立刻炸掉。
        if value not in ALL_ROLES:
            raise ValueError(f"必须是 {list(ALL_ROLES)} 之一")
        return value


class UserPage(BaseModel):
    items: list[UserAdminRead]
    total: int
    limit: int
    offset: int
