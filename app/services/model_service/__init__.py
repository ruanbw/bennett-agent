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

## 为什么拆成包

这一块原本是一个 1100 行的模块，混了三种独立的变化原因，各自的改动会互相踩：

- `registry`：管理后台的注册表 CRUD 与同步，以及对话请求的模型解析；
- `gateway`：旧版环境变量网关的拉取 / 解析 / 缓存 / single-flight；
- `outbound_guard`：出站地址守卫（元数据/内网拒绝，以及 http + 公网 key 的告警）；
- `errors`：服务层能给出的确定结论，供上面三块共用而不互相导入。

本模块**原样 re-export** 拆分前的全部公开 API 与测试引用的私有名，
`from app.services import model_service` 之后的 `model_service.XXX` 访问方式不变。
保留这一层是为了让调用方（`token_budget`、`app/api/*`、`app/server.py`、`main.py`）
不必跟着搬家，也让「谁是权威」只在一处说明。

## 路由键

对外模型 id 是 `{provider.slug}/{model_id}`。不同提供商可以各自提供 `gpt-4o`，
只用 `model_id` 做键会互相覆盖；`slug` 生成后不随改名变化，用户已保存的选择不会
因为管理员改个名字就指向另一个模型。

## 为什么数据库路径不缓存

模型启停是管理员刚点完就要生效的操作。管理后台的写操作与对话请求不在同一个进程
生命周期里没有通知机制，任何 TTL 缓存都会造成「明明禁用了还能用」。两张表都很小，
每次请求多两条查询比缓存失效逻辑便宜得多。旧版网关路径的缓存保持原样（见 `gateway`）。
"""

import socket

from app.services.model_service import gateway, outbound_guard, registry
from app.services.model_service.errors import (
    ApiKeyRequiredError,
    DuplicateModelError,
    InvalidModelStateError,
    ProviderFetchError,
    ProviderNotFoundError,
)
from app.services.model_service.gateway import (
    FALLBACK_CACHE_TTL_SECONDS,
    MODELS_CACHE_TTL_SECONDS,
    MODELS_TIMEOUT_SECONDS,
    _build_models_url,
    _build_url,
    _fallback,
    _parse_int_field,
    _parse_models,
    _parse_reasoning_flag,
    _parse_reasoning_levels,
    reset_models_cache,
)
from app.services.model_service.outbound_guard import (
    _is_internal_address,
    _is_metadata_range,
    _reject_private_target,
    _warn_plaintext_key_over_public_http,
)
from app.services.model_service.registry import (
    DEFAULT_PROVIDER_NAME,
    PROVIDER_SYNC_TIMEOUT_SECONDS,
    REGISTRY_MARKER_KEY,
    ChatModelSnapshot,
    _build_target,
    _default_display_name,
    _enabled_rows,
    _find_duplicate_model_id,
    _load_provider,
    _mark_registry_initialized,
    _provider_config_exists,
    _unique_provider_slug,
    create_provider,
    create_provider_model,
    delete_provider,
    delete_provider_model,
    fetch_available_models,
    get_default_model_key,
    get_known_model_ids,
    get_model_limits,
    get_provider,
    list_provider_models,
    list_providers,
    make_model_key,
    provider_config_exists,
    resolve_chat_model,
    resolve_model_target,
    seed_default_provider,
    slugify_provider_name,
    sync_provider_models,
    test_provider_connection,
    update_provider,
    update_provider_model,
)

#: 列在 `__all__` 里才算「有意导出」：这些私有名被 `token_budget` 与 tests/ 直接
#: 引用，拆分后必须仍然可达，否则调用方要跟着改导入路径（而 `token_budget` 不归
#: 本次改动范围）。`socket` 是模块对象本身——测试用
#: `monkeypatch.setattr(model_service.socket, "getaddrinfo", ...)` 替换 DNS，
#: 补丁打在同一个模块对象上，因此对 `outbound_guard` 同样生效。
__all__ = [
    "DEFAULT_PROVIDER_NAME",
    "FALLBACK_CACHE_TTL_SECONDS",
    "MODELS_CACHE_TTL_SECONDS",
    "MODELS_TIMEOUT_SECONDS",
    "PROVIDER_SYNC_TIMEOUT_SECONDS",
    "REGISTRY_MARKER_KEY",
    "ApiKeyRequiredError",
    "ChatModelSnapshot",
    "DuplicateModelError",
    "InvalidModelStateError",
    "ProviderFetchError",
    "ProviderNotFoundError",
    "_build_models_url",
    "_build_target",
    "_build_url",
    "_default_display_name",
    "_enabled_rows",
    "_find_duplicate_model_id",
    "_fallback",
    "_is_internal_address",
    "_is_metadata_range",
    "_load_provider",
    "_mark_registry_initialized",
    "_parse_int_field",
    "_parse_models",
    "_parse_reasoning_flag",
    "_parse_reasoning_levels",
    "_provider_config_exists",
    "_reject_private_target",
    "_unique_provider_slug",
    "_warn_plaintext_key_over_public_http",
    "create_provider",
    "create_provider_model",
    "delete_provider",
    "delete_provider_model",
    "fetch_available_models",
    "gateway",
    "get_default_model_key",
    "get_known_model_ids",
    "get_model_limits",
    "get_provider",
    "list_provider_models",
    "list_providers",
    "make_model_key",
    "outbound_guard",
    "provider_config_exists",
    "registry",
    "reset_models_cache",
    "resolve_chat_model",
    "resolve_model_target",
    "seed_default_provider",
    "slugify_provider_name",
    "socket",
    "sync_provider_models",
    "test_provider_connection",
    "update_provider",
    "update_provider_model",
]
