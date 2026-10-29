"""模型服务的对外出口：只导出对外契约，私有实现留在各子模块里。

导出面 = 管理端与对话路径真正调用的入口 + 有明确语义的公开查询（默认模型、白名单、窗口上限）。
拆包只按变化原因分：`provider_repo`（行级读写）、`provider_sync`（写用例与上游同步）、
`registry`（模型解析与对外清单）、`gateway`（旧版环境变量兜底）、`outbound_guard`（出站守卫）、
`upstream`（上游响应解析）、`presets`（厂商预设）、`errors`（错误类型）。
要验证内部实现就去它的定义处引用：`__all__` 不是内部实现的冻结契约。
"""

from app.services.model_service.errors import (
    ApiKeyRequiredError,
    DuplicateModelError,
    InvalidModelStateError,
    ProviderFetchError,
    ProviderNotFoundError,
)
from app.services.model_service.outbound_guard import reject_private_target
from app.services.model_service.presets import (
    AUTH_STYLES,
    get_preset,
    list_presets,
)
from app.services.model_service.provider_sync import (
    create_provider,
    create_provider_model,
    delete_provider,
    delete_provider_model,
    get_provider,
    list_provider_models,
    list_providers,
    seed_default_provider,
    sync_provider_models,
    test_provider_connection,
    update_provider,
    update_provider_model,
)
from app.services.model_service.registry import (
    ChatModelSnapshot,
    fetch_available_models,
    get_default_model_key,
    get_known_model_ids,
    get_model_limits,
    make_model_key,
    provider_config_exists,
    resolve_chat_model,
    resolve_model_target,
)

__all__ = [
    "AUTH_STYLES",
    "ApiKeyRequiredError",
    "ChatModelSnapshot",
    "DuplicateModelError",
    "InvalidModelStateError",
    "ProviderFetchError",
    "ProviderNotFoundError",
    "create_provider",
    "create_provider_model",
    "delete_provider",
    "delete_provider_model",
    "fetch_available_models",
    "get_default_model_key",
    "get_known_model_ids",
    "get_model_limits",
    "get_preset",
    "get_provider",
    "list_presets",
    "list_provider_models",
    "list_providers",
    "make_model_key",
    "provider_config_exists",
    "reject_private_target",
    "resolve_chat_model",
    "resolve_model_target",
    "seed_default_provider",
    "sync_provider_models",
    "test_provider_connection",
    "update_provider",
    "update_provider_model",
]
