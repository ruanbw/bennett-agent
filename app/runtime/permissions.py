"""权限（capability）目录：用户预先允许 Agent 做什么，以及工具需要哪些权限。

权限与 `RiskLevel` 正交，不能互相替代：

- `RiskLevel` 回答「这次调用有多不可逆」，用于 UI 提示与兜底拦截；
- `Permission` 回答「用户是否已经预先授权这一类能力」，用于把「每次点批准」变成一次选择。

工具声明 `permissions`（例如 `sandbox_exec` 需要 `execute`），客户端在每条消息里带上当前
会话的授权集合。审批闸门只在「工具所需权限未被全部授予」时拦下调用——这就是「在输入框
选择权限，Agent 执行时按需申请」的服务端落点。

权限名是稳定的线上契约：前端存进 localStorage、请求体原样回传，不能随意改名。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Permission(str, Enum):
    """可授予的能力类别。值即线上键名（前端 localStorage 与请求体共用）。"""

    READ = "read"
    """读取数据：读取文件、联网检索等只读操作。"""

    WRITE = "write"
    """写入文件：创建、修改或删除隔离环境中的文件。"""

    EXECUTE = "execute"
    """执行命令：运行 shell、脚本、安装依赖。"""

    NETWORK = "network"
    """联网访问：搜索引擎、网页抓取与外部网络接口。"""

    EXTERNAL = "external"
    """外部工具：MCP 第三方 server 提供的工具。"""

    MANAGE = "manage"
    """环境管理：重置或销毁隔离执行环境。"""

    DANGEROUS = "dangerous"
    """高风险操作：不可恢复、默认永远逐次询问的调用。"""


@dataclass(frozen=True)
class PermissionSpec:
    """一个权限的展示信息。前端直接渲染，不在客户端另写一份文案。"""

    key: Permission
    label: str
    description: str
    #: 仅用于选择器的视觉分级：`low` / `medium` / `high`。真正的执行闸门是 RiskLevel。
    risk: str = "low"


#: 权限目录的唯一事实来源。增删权限必须同步 `tool_registry` 的工具声明与前端测试。
PERMISSION_SPECS: tuple[PermissionSpec, ...] = (
    PermissionSpec(
        key=Permission.READ,
        label="读取数据",
        description="读取工作区文件、搜索结果等只读内容，不会修改任何东西",
        risk="low",
    ),
    PermissionSpec(
        key=Permission.WRITE,
        label="写入文件",
        description="创建、修改或删除隔离环境中的文件，删除后无法恢复",
        risk="medium",
    ),
    PermissionSpec(
        key=Permission.EXECUTE,
        label="执行命令",
        description="在隔离环境中运行 shell 命令、脚本或安装依赖；命令本身可以产生任意副作用",
        risk="medium",
    ),
    PermissionSpec(
        key=Permission.NETWORK,
        label="联网访问",
        description="访问互联网、调用搜索引擎或外部网络接口",
        risk="low",
    ),
    PermissionSpec(
        key=Permission.EXTERNAL,
        label="外部工具",
        description="调用 MCP 第三方工具；行为与数据流向由外部 server 决定",
        risk="medium",
    ),
    PermissionSpec(
        key=Permission.MANAGE,
        label="环境管理",
        description="重置或销毁隔离执行环境；已安装的依赖与工作区文件都会丢失",
        risk="high",
    ),
    PermissionSpec(
        key=Permission.DANGEROUS,
        label="高风险操作",
        description="不可恢复的破坏性操作；开启后此类调用不再逐次询问，请只在隔离环境中使用",
        risk="high",
    ),
)

PERMISSION_BY_KEY: dict[str, PermissionSpec] = {spec.key.value: spec for spec in PERMISSION_SPECS}
PERMISSION_LABELS: dict[str, str] = {spec.key.value: spec.label for spec in PERMISSION_SPECS}

#: 未显式传权限时的默认授权：只读 + 联网，与加入权限前的行为一致。
#: 写入、执行、外部工具、环境管理与高风险操作默认都需要逐次批准。
DEFAULT_PERMISSION_KEYS: tuple[str, ...] = (Permission.READ.value, Permission.NETWORK.value)
DEFAULT_PERMISSIONS: frozenset[str] = frozenset(DEFAULT_PERMISSION_KEYS)


@dataclass(frozen=True)
class PermissionPreset:
    """输入框权限选择器的一档预设。

    `auto` 刻意不包含 `manage` 与 `dangerous`：即便用户选择「自动执行」，重置执行环境与
    高风险操作也应逐次确认。要连它们一起放行，只能进入自定义并单独勾选（前端会给出红色警示）。
    """

    key: str
    label: str
    description: str
    permissions: tuple[str, ...]


#: 预设顺序即前端菜单顺序。`custom` 不是服务端预设：它由任意勾选组合派生。
PERMISSION_PRESETS: tuple[PermissionPreset, ...] = (
    PermissionPreset(
        key="ask",
        label="每次询问",
        description="只读与联网自动执行；写入、执行命令、外部工具逐次询问",
        permissions=DEFAULT_PERMISSION_KEYS,
    ),
    PermissionPreset(
        key="standard",
        label="标准",
        description="读取、写入与联网自动执行；执行命令与外部工具逐次询问",
        permissions=(Permission.READ.value, Permission.WRITE.value, Permission.NETWORK.value),
    ),
    PermissionPreset(
        key="auto",
        label="自动执行",
        description="除环境管理与高风险操作外全部自动执行；首次使用建议先看一遍工具轨迹",
        permissions=(
            Permission.READ.value,
            Permission.WRITE.value,
            Permission.EXECUTE.value,
            Permission.NETWORK.value,
            Permission.EXTERNAL.value,
        ),
    ),
)

PRESET_BY_KEY: dict[str, PermissionPreset] = {preset.key: preset for preset in PERMISSION_PRESETS}

#: 工具元数据里存所需权限的键（`ManagedTool` 构造时写入，MCP 注解层也会补）。
PERMISSIONS_METADATA_KEY = "permissions"


def permission_label(key: str) -> str:
    """取权限的中文名；未知键原样返回，保证 UI 不会出现空白徽标。"""
    spec = PERMISSION_BY_KEY.get(key)
    return spec.label if spec is not None else key


def permission_labels(keys: Iterable[str]) -> list[str]:
    """按传入顺序把权限键翻成中文名（去重）。"""
    seen: set[str] = set()
    labels: list[str] = []
    for key in keys:
        if key in seen:
            continue
        seen.add(key)
        labels.append(permission_label(key))
    return labels


def validate_permission_keys(values: Sequence[str]) -> list[str]:
    """校验并归一化请求里的权限键（去重保序）。

    未知键直接拒绝而不是静默忽略：拼错的权限名会让用户以为已授权、实际每次都被拦下，
    这种「界面显示已开、行为仍询问」的错位比 422 难排查得多。
    """
    normalized: list[str] = []
    seen: set[str] = set()
    unknown: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        if value not in PERMISSION_BY_KEY:
            unknown.append(value)
            continue
        normalized.append(value)
    if unknown:
        allowed = "、".join(spec.key.value for spec in PERMISSION_SPECS)
        raise ValueError(f"未知的权限 {unknown[0]!r}；可选权限：{allowed}")
    return normalized


def resolve_granted_permissions(values: Iterable[str] | None) -> frozenset[str]:
    """把请求里的权限列表解析成授权集合；`None` 表示客户端未指定，使用默认集合。

    未知键在这里按「未授予」处理而不是抛错：这个函数也服务于老客户端与内部调用，
    权限判定必须失败关闭，但不该让整轮对话因为一个旧键名而 422。
    """
    if values is None:
        return DEFAULT_PERMISSIONS
    granted: set[str] = set()
    for value in values:
        if value in PERMISSION_BY_KEY:
            granted.add(value)
    return frozenset(granted)


def permissions_from_metadata(metadata: Mapping[str, Any] | None) -> tuple[str, ...]:
    """从工具 metadata 读所需权限；缺失或形状不对时返回空元组。

    空元组不等于「不需要审批」：风险等级仍会参与判定（见 `app/runtime/approval.py`）。
    未知权限键保留原样，审批闸门会把它当作「永远未授予」，从而失败关闭。
    """
    if not metadata:
        return ()
    raw = metadata.get(PERMISSIONS_METADATA_KEY)
    if isinstance(raw, Permission):
        return (raw.value,)
    if isinstance(raw, str):
        return (raw,)
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return ()
    result: list[str] = []
    for value in raw:
        if isinstance(value, Permission):
            result.append(value.value)
        elif isinstance(value, str) and value:
            result.append(value)
    return tuple(dict.fromkeys(result))


def missing_permissions(required: Iterable[str], granted: Iterable[str]) -> tuple[str, ...]:
    """返回尚未授予的权限（保持 required 的顺序，便于提示第一个缺口）。"""
    granted_set = set(granted)
    return tuple(key for key in dict.fromkeys(required) if key not in granted_set)


@dataclass(frozen=True)
class AgentRunDeps:
    """一轮对话的运行时依赖：当前会话的授权集合。

    放在 deps 而不是 ContextVar：pydantic-ai 的 `RunContext.deps` 是显式传参，测试与
    并发请求都不会共享隐式状态；审批闸门通过 `ctx.deps.permissions` 读取。
    """

    permissions: frozenset[str] = field(default_factory=lambda: DEFAULT_PERMISSIONS)


__all__ = [
    "AgentRunDeps",
    "DEFAULT_PERMISSIONS",
    "DEFAULT_PERMISSION_KEYS",
    "PERMISSIONS_METADATA_KEY",
    "PERMISSION_BY_KEY",
    "PERMISSION_LABELS",
    "PERMISSION_PRESETS",
    "PERMISSION_SPECS",
    "PRESET_BY_KEY",
    "Permission",
    "PermissionPreset",
    "PermissionSpec",
    "missing_permissions",
    "permission_label",
    "permission_labels",
    "permissions_from_metadata",
    "resolve_granted_permissions",
    "validate_permission_keys",
]
