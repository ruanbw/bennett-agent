"""运行时能力探针（`app/api/capabilities.py`）。

前端空对话页的起步建议靠这份清单决定「能不能推荐联网搜索」：清单说 false 时多展示
一条搜索示例，用户点开只会得到「未配置」。因此这里的断言不是「配置项读没读到」，
而是**实际装配结果**——`build_tools()` 不挂 tavily，能力就必须是 false。
"""

import pytest

from app.api.capabilities import collect_capabilities
from app.config import is_usable_api_key, settings


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", "tvly-your-api-key", "tvly-dev-your-key-here", "CHANGEME", "replace-me", "placeholder"],
)
def test_is_usable_api_key_rejects_missing_and_placeholders(value):
    """占位值非空，却与没配一样必然失败：不能把它当成「已配置」。"""
    assert is_usable_api_key(value) is False


@pytest.mark.parametrize("value", ["tvly-test-key", "tvly-dev-abc123", "  sk-live-7f3a  "])
def test_is_usable_api_key_accepts_real_looking_keys(value):
    """只判占位，不猜格式：各上游 key 形状不同，多校验只会误杀。"""
    assert is_usable_api_key(value) is True


def test_capabilities_without_search_key(monkeypatch):
    monkeypatch.setattr(settings, "tavily_api_key", None)

    capabilities = collect_capabilities()

    assert capabilities.web_search is False
    assert capabilities.tools == ["ask_user_clarification", "get_current_time"]


def test_capabilities_with_placeholder_key_do_not_claim_search(monkeypatch):
    """`.env.example` 的占位值不能让能力探针谎报「可以联网」。"""
    monkeypatch.setattr(settings, "tavily_api_key", "tvly-dev-your-key-here")

    capabilities = collect_capabilities()

    assert capabilities.web_search is False
    assert "tavily_search" not in capabilities.tools


def test_capabilities_with_configured_key(monkeypatch):
    monkeypatch.setattr(settings, "tavily_api_key", "tvly-test-key")

    capabilities = collect_capabilities()

    assert capabilities.web_search is True
    # 工具顺序即注册顺序；前端只读 web_search，列表留给排障
    assert capabilities.tools == ["ask_user_clarification", "get_current_time", "tavily_search"]


def test_sandbox_capability_follows_the_switch(monkeypatch):
    """沙箱开关同时决定「工具挂不挂」与「`sandbox` 报不报 true」。

    这两件事必须由同一个判据派生：单独改一处会出现「前端推荐跑代码、模型却没有工具」
    或者反过来。
    """
    monkeypatch.setattr(settings, "sandbox_enabled", True)

    enabled = collect_capabilities()

    assert enabled.sandbox is True
    assert {"sandbox_exec", "sandbox_write_file", "sandbox_read_file", "sandbox_reset"} <= set(enabled.tools)

    monkeypatch.setattr(settings, "sandbox_enabled", False)
    disabled = collect_capabilities()
    assert disabled.sandbox is False
    assert not any(name.startswith("sandbox_") for name in disabled.tools)
