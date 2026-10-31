"""权限目录与「风险等级 + 预授权」判定的行为锁。

这一层的关键性质：

1. **权限键是线上契约**：前端 localStorage、请求体、工具 metadata 三处共用同一组键，
   改名或漏声明都会让用户勾了权限却仍被逐次拦截；
2. **失败关闭**：未知权限、缺失元数据、未知风险等级都不能默认放行；
3. **预授权只覆盖对应能力**：授予 `read` 不能让写文件跳过审批。
"""

from __future__ import annotations

import pytest

from app.runtime.approval import requires_approval
from app.runtime.permissions import (
    DEFAULT_PERMISSION_KEYS,
    DEFAULT_PERMISSIONS,
    PERMISSION_BY_KEY,
    PERMISSION_PRESETS,
    PERMISSION_SPECS,
    Permission,
    missing_permissions,
    permission_labels,
    permissions_from_metadata,
    resolve_granted_permissions,
    validate_permission_keys,
)
from app.runtime.tool_registry import RiskLevel


def test_permission_keys_are_the_stable_wire_contract():
    """键名一旦发出去就会存进浏览器、写进请求体；这组值不能悄悄变。"""
    assert {spec.key.value for spec in PERMISSION_SPECS} == {
        "read",
        "write",
        "execute",
        "network",
        "external",
        "manage",
        "dangerous",
    }
    assert len(PERMISSION_BY_KEY) == len(PERMISSION_SPECS)


def test_every_spec_has_user_facing_copy():
    """选择器直接渲染这些文案；空标签或空说明等于让用户盲选。"""
    for spec in PERMISSION_SPECS:
        assert spec.label.strip()
        assert spec.description.strip()
        assert spec.risk in {"low", "medium", "high"}


def test_default_permissions_are_read_only_and_network():
    """默认行为必须与加入权限功能前一致：只读 + 联网自动，写入/执行逐次问。"""
    assert frozenset(DEFAULT_PERMISSION_KEYS) == DEFAULT_PERMISSIONS
    assert {Permission.READ.value, Permission.NETWORK.value} == DEFAULT_PERMISSIONS


def test_presets_are_valid_and_auto_keeps_dangerous_asking():
    """预设只能引用目录里的权限；「自动执行」也绝不能顺手放行 dangerous。"""
    valid = set(PERMISSION_BY_KEY)
    for preset in PERMISSION_PRESETS:
        assert preset.permissions, f"预设 {preset.key} 为空"
        assert set(preset.permissions) <= valid

    auto = next(preset for preset in PERMISSION_PRESETS if preset.key == "auto")
    assert Permission.DANGEROUS.value not in auto.permissions
    # 重置执行环境会丢掉已装依赖与文件，不能藏在「自动执行」里
    assert Permission.MANAGE.value not in auto.permissions
    assert Permission.EXECUTE.value in auto.permissions


def test_validate_rejects_unknown_permission():
    """拼错的权限名要 422，而不是「界面显示已开、实际每次仍被拦」。"""
    with pytest.raises(ValueError, match="未知的权限"):
        validate_permission_keys(["read", "execut"])


def test_validate_dedupes_and_keeps_order():
    assert validate_permission_keys(["execute", "read", "execute"]) == ["execute", "read"]


def test_resolve_none_uses_default():
    assert resolve_granted_permissions(None) == DEFAULT_PERMISSIONS


def test_resolve_ignores_unknown_keys_fail_closed():
    """内部调用遇到旧键名不能 422，但未知键也绝不能变成授权。"""
    granted = resolve_granted_permissions(["read", "future_permission"])
    assert granted == {"read"}


def test_missing_permissions_reports_first_gaps_in_order():
    assert missing_permissions(("execute", "write", "execute"), ("read",)) == ("execute", "write")


def test_permission_labels_are_deduplicated_and_unknown_passthrough():
    assert permission_labels(["execute", "execute", "future"]) == ["执行命令", "future"]


def test_permissions_from_metadata_accepts_strings_and_enums():
    assert permissions_from_metadata({"permissions": ["write", Permission.EXECUTE]}) == ("write", "execute")
    assert permissions_from_metadata({"permissions": "read"}) == ("read",)
    assert permissions_from_metadata(None) == ()
    assert permissions_from_metadata({"permissions": 42}) == ()


@pytest.mark.parametrize(
    ("required", "granted", "expected"),
    [
        # 已授予对应权限：写入工具不再逐次询问
        (("execute",), {"execute"}, False),
        # 授予了别的权限：仍要询问
        (("write",), {"read", "network"}, True),
        # 只读工具也会被「用户关掉了联网」拦住（权限是用户显式选择）
        (("network",), {"read"}, True),
        # 没有权限要求但风险等级是写入：仍要询问
        ((), {"read", "write"}, True),
    ],
)
def test_requires_approval_combines_risk_and_permissions(required, granted, expected):
    assert (
        requires_approval(
            RiskLevel.SAFE_WRITE,
            required_permissions=required,
            granted_permissions=granted,
        )
        is expected
    )


def test_read_only_with_permission_granted_does_not_ask():
    assert (
        requires_approval(
            RiskLevel.READ_ONLY,
            required_permissions=("read",),
            granted_permissions={"read"},
        )
        is False
    )


def test_missing_risk_still_asks_even_when_permissions_granted():
    """权限不能覆盖「不知道风险等级」：未知即拦的默认值必须保留。"""
    assert (
        requires_approval(
            None,
            required_permissions=("execute",),
            granted_permissions={"execute"},
        )
        is True
    )


def test_every_builtin_tool_declares_known_permissions():
    """新增工具漏写 permissions 时红：否则它只能永远弹审批框，或者被误当成无权限要求。"""
    from app.runtime.permissions import PERMISSIONS_METADATA_KEY
    from app.tools import build_tools

    tools = build_tools()
    assert tools
    valid = set(PERMISSION_BY_KEY)
    for tool in tools:
        assert set(tool.permissions) <= valid, f"{tool.name} 声明了未知权限 {tool.permissions}"
        # 属性与 metadata 必须一致：闸门读 metadata，审批卡读 catalog（最终也读 metadata）。
        assert list(tool.metadata.get(PERMISSIONS_METADATA_KEY, [])) == list(tool.permissions), (
            f"{tool.name} 的 permissions 属性与 metadata 不一致"
        )

    # 有副作用的工具必须至少声明一个权限，否则「预授权」对它们完全无效。
    for tool in tools:
        if tool.risk_level in {RiskLevel.SAFE_WRITE, RiskLevel.DANGEROUS}:
            assert tool.permissions, f"{tool.name} 是写入类工具但没有声明权限"


def test_capabilities_exposes_the_same_permission_catalog():
    """前端选择器从能力探针取目录；它必须与闸门读的目录同源。"""
    from app.api.capabilities import collect_capabilities

    capabilities = collect_capabilities()
    assert [option.key for option in capabilities.permissions] == [spec.key.value for spec in PERMISSION_SPECS]
    assert [preset.key for preset in capabilities.permission_presets] == [preset.key for preset in PERMISSION_PRESETS]
    assert capabilities.default_permissions == list(DEFAULT_PERMISSION_KEYS)


def test_chat_request_rejects_unknown_permission():
    from app.schemas.chat import ChatRequest

    with pytest.raises(ValueError, match="未知的权限"):
        ChatRequest(messages=[{"role": "user", "content": "hi"}], permissions=["execute", "shell"])


def test_chat_request_accepts_explicit_empty_permissions():
    """`[]` 与 `None` 语义不同：前者是「什么都不预先授权」。"""
    from app.schemas.chat import ChatRequest

    request = ChatRequest(messages=[{"role": "user", "content": "hi"}], permissions=[])
    assert request.permissions == []
