"""管理后台的请求/响应模型。

几条刻意的边界：

1. **API key 只进不出**：响应里永远只有掩码（`api_key_hint`），编辑时留空表示
   「不修改」。没有「回显完整 key」的字段，避免它出现在浏览器缓存、日志、
   截图里。
2. **base_url 只接受 http(s)**：管理接口本身只有超级管理员能访问，但错误协议
   （`file://`、`javascript:`）没有任何合法用途，早失败比交给 httpx 报一个难懂的
   错更好。
3. 更新用 `None`/缺省表示「不修改」，与 `model_service` 的哨兵语义对齐：
   `ProviderUpdate` 的字段全部可选，请求里没出现的键不会被写回。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

MAX_NAME_CHARS = 80
MAX_URL_CHARS = 500
MAX_API_KEY_CHARS = 1000
MAX_MODEL_ID_CHARS = 200


def mask_api_key(api_key: str | None) -> str | None:
    """把 API key 变成可安全展示的掩码。

    长度很短时不保留任何字符：`sk-1` 本身就没有可打码的余地，露出 2 个字符等于
    泄露一半。
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


class ProviderCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=MAX_NAME_CHARS)
    base_url: str = Field(..., min_length=1, max_length=MAX_URL_CHARS)
    api_key: str | None = Field(None, max_length=MAX_API_KEY_CHARS)
    enabled: bool = True

    @field_validator("name", "base_url")
    @classmethod
    def _strip(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("不能为空白")
        return cleaned

    @field_validator("base_url")
    @classmethod
    def _require_http_url(cls, value: str) -> str:
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


class ProviderUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=MAX_NAME_CHARS)
    base_url: str | None = Field(None, min_length=1, max_length=MAX_URL_CHARS)
    #: None = 不修改；空串 = 清空；非空 = 更新。用 `model_fields_set` 区分
    #: 「没传」与「显式传 null」，因此路由层只把请求里真正出现的键往下传。
    api_key: str | None = Field(None, max_length=MAX_API_KEY_CHARS)
    enabled: bool | None = None

    @field_validator("name", "base_url")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("不能为空白")
        return cleaned

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


class ModelCreate(BaseModel):
    model_id: str = Field(..., min_length=1, max_length=MAX_MODEL_ID_CHARS)
    display_name: str | None = Field(None, max_length=MAX_MODEL_ID_CHARS)
    description: str | None = Field(None, max_length=1000)
    reasoning: bool | None = None
    supported_reasoning_levels: list[str] = Field(default_factory=list)
    context_window: int | None = Field(None, ge=1)
    max_output_tokens: int | None = Field(None, ge=1)
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
    context_window: int | None = Field(None, ge=1)
    max_output_tokens: int | None = Field(None, ge=1)
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


class UserPage(BaseModel):
    items: list[UserAdminRead]
    total: int
    limit: int
    offset: int
