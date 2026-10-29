"""上游「列出模型」响应的统一解析：信封形状与字段别名在这里收敛成一份白名单。

同步（`provider_sync._fetch_remote_models`）与旧版网关（`gateway.parse_gateway_models`）共用本模块。
别名表外的字段不读：窗口上限会被 token 预算拿去截断请求，编一个数比留 None 更危险。
"""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UpstreamModel:
    """上游返回的一个模型条目：白名单字段 + 原始 JSON。

    `description` 已走完回退链，调用方拿到即可入库。`raw` 只做展示，永不参与运行时判断。
    """

    model_id: str
    display_name: str | None
    description: str | None
    owned_by: str
    reasoning: bool | None
    supported_reasoning_levels: list[str] = field(default_factory=list)
    context_window: int | None = None
    max_output_tokens: int | None = None
    raw: dict = field(default_factory=dict)  # noqa: ANN001 - 上游条目原样保存


def _extract_entries(payload: object) -> list[object]:
    """从响应里取出条目数组。支持三种信封：`{"data": [...]}`、`{"models": [...]}`、裸数组。"""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "models"):
            entries = payload.get(key)
            if isinstance(entries, list):
                return entries
    return []


def _parse_int_field(value: object) -> int | None:
    """宽松解析 int 型字段：int 或数字串，其余（None、'128k'、浮点）一律 None。

    不做单位换算：把 '128k' 猜成 128000 是伪造数据，前端会拿编出来的窗口做判断。
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


def _parse_reasoning_flag(item: dict) -> bool | None:  # noqa: ANN001 - 上游条目
    """reasoning 三态：只有上游给出的 bool 才算表态，其余（缺字段、类型不符）为 None。

    None 与 False 必须分开：False 让前端隐藏思考等级选择器，None 是「上游没说」。
    """
    value = item.get("reasoning")
    return value if isinstance(value, bool) else None


def parse_reasoning_levels(raw: object) -> list[str]:
    """解析模型支持的思考等级；两种形状都接受（`[{"effort": "high"}]` 或 `["high"]`）。

    其它形状丢掉：伪造等级会让用户选完请求失败（`{"effort": null}` 经 `str()` 会变成 "None"）。
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


def _first_int(item: dict, paths: tuple[tuple[str, ...], ...]) -> int | None:  # noqa: ANN001
    """按别名顺序取第一个能解析成 int 的值；嵌套路径用点前缀表达。"""
    for path in paths:
        value: object = item
        for key in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(key)
        parsed = _parse_int_field(value)
        if parsed is not None:
            return parsed
    return None


#: 模型 id 的候选键：OpenAI 系是 `id`，Ollama 原生用 `name`/`model`，Gemini 原生只有
#: 带 `models/` 前缀的 `name`。
_ID_KEYS = ("id", "model", "name")

#: 展示名的候选键，按明确程度排序。`name` 排最后：它在 Ollama/Gemini 原生就是 id 本身，
#: 与 id 同源时不算展示名（见 `_extract_display_name`）。
_DISPLAY_NAME_KEYS = ("display_name", "displayName")
_DISPLAY_FALLBACK_KEYS = ("name",)

#: 上下文窗口的别名，按可信度排序：`top_provider.context_length` 是 OpenRouter 实际生效的
#: 上限，比自报的 `context_length` 更保守。
_CONTEXT_WINDOW_PATHS: tuple[tuple[str, ...], ...] = (
    ("context_window",),
    ("top_provider", "context_length"),
    ("context_length",),
    ("max_context_length",),
    ("max_input_tokens",),
    ("inputTokenLimit",),
    ("max_model_len",),
    ("token_limits", "context_window"),
    ("contextLength",),
)

#: 最大输出 tokens 的别名。
_MAX_OUTPUT_PATHS: tuple[tuple[str, ...], ...] = (
    ("max_output_tokens",),
    ("max_completion_tokens",),
    ("top_provider", "max_completion_tokens"),
    ("outputTokenLimit",),
    ("token_limits", "max_output_token_length"),
    ("max_tokens",),
)

#: `supported_parameters` 里出现这些参数名说明模型支持思考输出（OpenRouter 没有布尔位）。
_REASONING_PARAMETER_NAMES = frozenset({"reasoning", "include_reasoning", "reasoning_effort"})


def _extract_model_id(item: dict) -> tuple[str, str]:  # noqa: ANN001
    """取出（上游模型 id, 产生它的原始字符串）；Gemini 原生的 `models/` 前缀在这里剥掉。

    `id` 存在但不是字符串视为坏条目（抛错告警），而不是静默跳过：那几乎必然意味着解析
    假设出了问题。原始字符串随 id 返回，因为判断展示名要与剥前缀之前的原值比较。
    """
    for key in _ID_KEYS:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            raw_value = value.strip()
            cleaned = raw_value
            if cleaned.startswith("models/"):
                cleaned = cleaned[len("models/") :]
            if cleaned:
                return cleaned, raw_value
        elif key == "id" and value is not None:
            raise ValueError(f"id 不是字符串: {value!r}")
    raise ValueError("条目里没有可用的模型 id")


def _extract_display_name(item: dict, id_raw_value: str) -> str | None:  # noqa: ANN001
    """展示名；`name` 与 id 同源时不算（Ollama/Gemini 原生的 name 就是 id）。

    `display_name`/`displayName` 存在但不是字符串视为坏条目（抛错）；空串按缺字段处理。
    """
    for key in (*_DISPLAY_NAME_KEYS, *_DISPLAY_FALLBACK_KEYS):
        if key not in item:
            continue
        value = item[key]
        if not isinstance(value, str):
            if key in _DISPLAY_NAME_KEYS:
                raise ValueError(f"{key} 不是字符串: {value!r}")
            continue
        cleaned = value.strip()
        if not cleaned:
            continue
        if cleaned == id_raw_value:
            continue
        return cleaned
    return None


def _extract_owned_by(item: dict, model_id: str) -> str:  # noqa: ANN001
    """`owned_by`：缺失时从 id 的命名空间推导（`openai/gpt-4o` -> `openai`）。"""
    value = item.get("owned_by")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return model_id.split("/")[0] if "/" in model_id else "Custom"


def _extract_description(
    item: dict,  # noqa: ANN001
    description_field: str,
    owned_by: str,
    display_name: str | None,
    model_id: str,
) -> str | None:
    """描述回退链：配置字段 -> `description` -> `owned_by 展示名`。

    `description_field` 由配置传入（`MODEL_DESCRIPTION_FIELD`）：展示标签字段名属于网关细节，
    不该硬编码在这里。
    """
    for key in (description_field, "description"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"{owned_by} {display_name or model_id}"


def _extract_reasoning(item: dict) -> bool | None:  # noqa: ANN001
    """思考能力：显式布尔位优先，其余只在明确给出证据时推导 True，绝不推导 False。

    各家表达不同（`supported_parameters` / `capabilities.thinking` / `supports_reasoning` /
    `effort.supported_levels`）；参数列表不完整是常态，拿「没列出」反推 False 会藏掉选择器。
    """
    flagged = _parse_reasoning_flag(item)
    if flagged is not None:
        return flagged

    params = item.get("supported_parameters")
    if isinstance(params, list) and {p for p in params if isinstance(p, str)} & _REASONING_PARAMETER_NAMES:
        return True

    capabilities = item.get("capabilities")
    if isinstance(capabilities, dict) and capabilities.get("thinking") is True:
        return True
    if isinstance(capabilities, list) and "thinking" in capabilities:
        return True
    if item.get("thinking") is True or item.get("supports_reasoning") is True:
        return True

    reasoning = item.get("reasoning")
    if isinstance(reasoning, dict):
        efforts = reasoning.get("supported_efforts")
        if isinstance(efforts, list) and efforts:
            return True

    effort = item.get("effort")
    if isinstance(effort, dict):
        levels = effort.get("supported_levels")
        if isinstance(levels, list) and levels:
            return True

    return None


def _parse_entry(item: dict, description_field: str) -> UpstreamModel:  # noqa: ANN001
    model_id, id_raw_value = _extract_model_id(item)
    display_name = _extract_display_name(item, id_raw_value)
    owned_by = _extract_owned_by(item, model_id)
    return UpstreamModel(
        model_id=model_id,
        display_name=display_name,
        description=_extract_description(item, description_field, owned_by, display_name, model_id),
        owned_by=owned_by,
        reasoning=_extract_reasoning(item),
        supported_reasoning_levels=parse_reasoning_levels(item.get("supported_reasoning_levels", [])),
        context_window=_first_int(item, _CONTEXT_WINDOW_PATHS),
        max_output_tokens=_first_int(item, _MAX_OUTPUT_PATHS),
        raw=item,
    )


def parse_upstream_models(payload: object, description_field: str) -> list[UpstreamModel]:
    """把上游响应解析成条目列表；无可用条目时返回空列表。

    容错做在条目级：某个模型的 id/展示名类型不符只丢那一条并留日志，其余照常。
    """
    parsed: list[UpstreamModel] = []
    for index, entry in enumerate(_extract_entries(payload)):
        if not isinstance(entry, dict):
            continue
        try:
            parsed.append(_parse_entry(entry, description_field))
        except Exception as exc:  # noqa: BLE001 - 单条坏数据不能作废整份列表
            logger.warning("跳过无法解析的模型条目 (index=%d, id=%r): %s", index, entry.get("id"), exc)
    return parsed


def next_page_cursor(payload: object) -> str | None:
    """读出「还有下一页」时的游标（Anthropic 的 `has_more` + `last_id`）。

    刻意只认这一种形状：认错游标等于对不支持分页的上游反复重打第一页。None 表示没有下一页。
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("has_more") is not True:
        return None
    last_id = payload.get("last_id")
    if not isinstance(last_id, str) or not last_id.strip():
        return None
    return last_id.strip()
