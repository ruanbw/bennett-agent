"""Tavily 搜索工具。

重写时拆出了 `_format_results` 并换成共享 httpx 客户端，但当时没有覆盖。这里补上，
重点是三类容易被改坏的行为：

1. 长度截断（单条 + 全文）——它同时是「防占满上下文」和「防恶意网页注入」的边界；
2. 失败时**返回文案给模型**而不是抛异常——工具失败要让模型看见原因并自行换策略；
3. 异常细节**不进上下文**（模型可能原样复述给用户），只进日志。
"""

import importlib

import httpx
import pytest

from app.config import settings
from app.tools.tavily_search import (
    MAX_ITEM_CHARS,
    MAX_TOTAL_CHARS,
    _format_results,
    tavily_search,
)

# 注意不能用 `from app.tools import tavily_search as module`：
# `app/tools/__init__.py` 里 re-export 了一个**同名函数**，会把子模块名遮蔽掉，
# 那样拿到的是函数而不是模块。
module = importlib.import_module("app.tools.tavily_search")


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = []

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.response


@pytest.fixture
def fake_http(monkeypatch):
    def install(response=None, exc=None):
        client = FakeClient(response=response, exc=exc)
        monkeypatch.setattr(module, "get_http_client", lambda: client)
        return client

    return install


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setattr(settings, "tavily_api_key", "tvly-test-key")


# ---------------------------------------------------------------------
# 未配置密钥
# ---------------------------------------------------------------------
async def test_missing_api_key_returns_instruction(monkeypatch):
    monkeypatch.setattr(settings, "tavily_api_key", None)

    result = await tavily_search(None, "anything")

    assert "未配置 Tavily API Key" in result


async def test_api_key_comes_only_from_settings(monkeypatch, fake_http):
    """回归（O-3）：此前还有一个 `os.getenv("TAVILY_API_KEY")` 回退。

    settings 已经通过 pydantic-settings 读同一个环境变量，那条回退是同一值的第二条
    配置路径——两条路径迟早会分叉（例如一个读了 .env、一个没读）。
    """
    monkeypatch.setattr(settings, "tavily_api_key", None)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-from-env")
    client = fake_http(FakeResponse(payload={"results": []}))

    result = await tavily_search(None, "q")

    assert "未配置" in result
    assert client.calls == [], "不该因为环境变量里有 key 就发请求"


# ---------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------
async def test_formats_answer_and_results(fake_http):
    fake_http(
        FakeResponse(
            payload={
                "answer": "快速答案",
                "results": [
                    {"title": "标题一", "url": "https://a.example", "content": "正文一"},
                    {"title": "标题二", "url": "https://b.example", "content": "正文二"},
                ],
            }
        )
    )

    result = await tavily_search(None, "关键词")

    assert "【快速摘要】: 快速答案" in result
    assert "1. 标题一" in result
    assert "https://a.example" in result
    assert "正文二" in result


async def test_reports_when_nothing_found(fake_http):
    fake_http(FakeResponse(payload={"results": []}))

    assert "未检索到相关网页内容。" in await tavily_search(None, "q")


async def test_answer_is_optional(fake_http):
    fake_http(FakeResponse(payload={"results": [{"title": "t", "url": "u", "content": "c"}]}))

    assert "【快速摘要】" not in await tavily_search(None, "q")


async def test_tolerates_missing_result_fields(fake_http):
    """上游字段缺失不能炸——工具返回错误文案会污染上下文。"""
    fake_http(FakeResponse(payload={"results": [{}]}))

    result = await tavily_search(None, "q")

    assert "无标题" in result


# ---------------------------------------------------------------------
# 长度截断
# ---------------------------------------------------------------------
def test_long_item_is_truncated():
    long_content = "x" * (MAX_ITEM_CHARS + 500)

    result = _format_results({"results": [{"title": "t", "url": "u", "content": long_content}]}, "q")

    assert "[已截断]" in result
    assert len(result) < len(long_content)


def test_item_at_limit_is_not_truncated():
    content = "x" * MAX_ITEM_CHARS

    result = _format_results({"results": [{"title": "t", "url": "u", "content": content}]}, "q")

    # 尾部只有渲染加的换行/空格，不该出现截断标记
    assert "[已截断]" not in result


def test_total_output_is_capped():
    many = [{"title": f"t{i}", "url": "u", "content": "y" * MAX_ITEM_CHARS} for i in range(50)]

    result = _format_results({"results": many}, "q")

    assert len(result) <= MAX_TOTAL_CHARS + len("\n... [搜索全文超出上限，已截断]")
    assert "搜索全文超出上限" in result


def test_total_cap_survives_all_content_in_answer():
    """摘要不计入单条上限，总上限必须兜住它。"""
    result = _format_results({"answer": "z" * (MAX_TOTAL_CHARS * 2), "results": []}, "q")

    assert "搜索全文超出上限" in result


def test_whitespace_in_content_is_stripped():
    result = _format_results({"results": [{"title": "t", "url": "u", "content": "  正文  "}]}, "q")

    assert "正文" in result
    assert "  正文  " not in result


# ---------------------------------------------------------------------
# 请求构造
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "asked,expected",
    [(0, 1), (-5, 1), (3, 3), (10, 10), (999, 10), (None, 5), ("invalid", 5)],
)
async def test_max_results_is_clamped(fake_http, asked, expected):
    """上限既防「要一万条」，也防负数把上游打回 400，并防 None/非整数导致 TypeError。"""
    client = fake_http(FakeResponse(payload={"results": []}))

    await tavily_search(None, "q", asked)

    assert client.calls[0][1]["json"]["max_results"] == expected


async def test_posts_to_tavily_with_timeout(fake_http):
    client = fake_http(FakeResponse(payload={"results": []}))

    await tavily_search(None, "q")

    url, kwargs = client.calls[0]
    assert url == module.TAVILY_SEARCH_URL
    assert kwargs["timeout"] == module.TAVILY_TIMEOUT_SECONDS
    assert kwargs["json"]["query"] == "q"


# ---------------------------------------------------------------------
# 失败路径
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    "status,expected",
    [(401, "认证失败"), (429, "请求过于频繁")],
)
async def test_known_error_statuses_have_specific_messages(fake_http, status, expected):
    fake_http(FakeResponse(status_code=status))

    assert expected in await tavily_search(None, "q")


async def test_other_error_status_includes_short_body(fake_http):
    fake_http(FakeResponse(status_code=500, text="x" * 500))

    result = await tavily_search(None, "q")

    assert "HTTP 500" in result
    # 只带一小段，不要把上游整个错误页塞进上下文
    assert len(result) < 400


async def test_timeout_has_its_own_message(fake_http):
    """超时与其它失败对模型是两种信号：前者值得换关键词重试。"""
    fake_http(exc=httpx.TimeoutException("slow"))

    assert "超时" in await tavily_search(None, "q")


async def test_unexpected_failure_does_not_leak_details(fake_http, caplog):
    secret = "internal-connection-detail-should-not-leak"
    fake_http(exc=RuntimeError(secret))

    with caplog.at_level("WARNING", logger="app.tools.tavily_search"):
        result = await tavily_search(None, "q")

    assert secret not in result
    assert "发生异常" in result
    # 细节进日志而不是进上下文
    assert secret in caplog.text


async def test_html_response_on_200_handled_gracefully(fake_http):
    """上游返回 200 却不是合法 JSON（如某些网关返回的 HTML 页面）时应友好兜底。"""

    class BadJsonResponse(FakeResponse):
        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    fake_http(BadJsonResponse(status_code=200, text="<html>502 Bad Gateway</html>"))

    result = await tavily_search(None, "q")
    assert "响应解析失败" in result
    assert "502 Bad Gateway" in result
