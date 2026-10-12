"""服务装配的回归测试：CORS、文档暴露面、lifespan 启动与关闭。

**统一用 `create_app()` 的返回值，而不是模块级 `app`**：`settings` 是单例，用例会
monkeypatch 它，而模块级 `app` 的中间件/路由在导入时就按当时的配置建好了——拿它
断言会看到陈旧配置，并且用例执行顺序一变就互相污染。
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app import server
from app.config import settings

#: 预检请求标头：真实浏览器发 CORS preflight 时就是这三个
PREFLIGHT_HEADERS = {
    "Origin": "http://localhost:5173",
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "content-type",
}


# ---------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------
def test_preflight_omits_credentials_header_by_default(monkeypatch):
    """allow_credentials 默认关闭：预检响应里不能出现 Allow-Credentials。

    带凭据的跨域会让浏览器在「来源通配」时直接拒绝响应，默认保持关闭才是安全的。
    """
    monkeypatch.setattr(settings, "cors_allowed_origins", "http://localhost:5173")
    monkeypatch.setattr(settings, "cors_allow_credentials", False)

    with TestClient(server.create_app()) as client:
        response = client.options("/api/chat", headers=PREFLIGHT_HEADERS)

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "access-control-allow-credentials" not in response.headers


def test_preflight_reports_credentials_when_enabled(monkeypatch):
    """settings.cors_allow_credentials=True 必须真的接到 CORSMiddleware 上。"""
    monkeypatch.setattr(settings, "cors_allowed_origins", "http://localhost:5173")
    monkeypatch.setattr(settings, "cors_allow_credentials", True)

    with TestClient(server.create_app()) as client:
        response = client.options("/api/chat", headers=PREFLIGHT_HEADERS)

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert response.headers["access-control-allow-credentials"] == "true"


@pytest.mark.parametrize("origins", ["*", "http://localhost:5173, *"])
def test_credentials_with_wildcard_origin_is_rejected(monkeypatch, origins):
    """通配来源 + 携带凭据会被浏览器拒绝，必须在装配时就报错而不是静默降级。"""
    monkeypatch.setattr(settings, "cors_allowed_origins", origins)
    monkeypatch.setattr(settings, "cors_allow_credentials", True)

    with pytest.raises(ValueError, match=r'不能包含 "\*"'):
        server.create_app()


# ---------------------------------------------------------------------
# 文档暴露面
# ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("app_env", "enable_api_docs", "expected_status"),
    [
        # None = 自动：production 关闭，其余环境开启
        ("production", None, 404),
        ("development", None, 200),
        # 显式配置覆盖自动判断
        ("production", True, 200),
        ("development", False, 404),
    ],
)
def test_api_docs_exposure(monkeypatch, app_env, enable_api_docs, expected_status):
    monkeypatch.setattr(settings, "app_env", app_env)
    monkeypatch.setattr(settings, "enable_api_docs", enable_api_docs)

    client = TestClient(server.create_app())
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == expected_status, path


# ---------------------------------------------------------------------
# lifespan
# ---------------------------------------------------------------------
def test_lifespan_runs_startup_cleanup(monkeypatch, caplog):
    """启动时清一次过期会话，并把删除条数写进日志。"""
    expected_lifetime = settings.session_lifetime_seconds
    calls: list[int] = []

    async def fake_cleanup(lifetime_seconds: int) -> int:
        calls.append(lifetime_seconds)
        return 3

    monkeypatch.setattr(server, "cleanup_expired_access_tokens", fake_cleanup)

    with caplog.at_level(logging.INFO, logger="app.server"), TestClient(server.create_app()):
        pass

    assert calls == [expected_lifetime]
    assert any("删除=3" in record.getMessage() for record in caplog.records)


def test_startup_cleanup_failure_does_not_block_startup(monkeypatch, caplog):
    """清理是 best-effort：抛错只记 warning，服务照常起来并响应请求。"""

    async def boom(_lifetime_seconds: int) -> int:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(server, "cleanup_expired_access_tokens", boom)

    with caplog.at_level(logging.WARNING, logger="app.server"), TestClient(server.create_app()) as client:
        # 能收到正常的 401（而非 500/启动崩溃），说明应用已就绪
        assert client.get("/api/auth/me").status_code == 401

    assert any("启动清理过期会话失败" in record.getMessage() for record in caplog.records)


def _patch_shutdown(monkeypatch, close_mock, dispose_mock):
    """替换关闭路径上的两个引用。

    `engine` 整体换成替身：`AsyncEngine.dispose` 是只读属性，无法就地 monkeypatch。
    """
    monkeypatch.setattr(server, "close_http_client", close_mock)
    monkeypatch.setattr(server, "engine", SimpleNamespace(dispose=dispose_mock))


def test_shutdown_disposes_http_client_and_engine(monkeypatch):
    """关闭阶段两个清理动作都要执行：漏了会泄漏连接、WAL 不合并。"""
    close_mock = AsyncMock()
    dispose_mock = AsyncMock()
    _patch_shutdown(monkeypatch, close_mock, dispose_mock)

    with TestClient(server.create_app()):
        pass

    close_mock.assert_awaited_once()
    dispose_mock.assert_awaited_once()


@pytest.mark.parametrize("failing", ["close_http_client", "engine.dispose"])
def test_shutdown_failures_do_not_mask_each_other(monkeypatch, caplog, failing):
    """一个清理动作抛错不能让另一个被跳过，也不能让关闭阶段崩掉。"""
    close_mock = AsyncMock(side_effect=RuntimeError("close boom") if failing == "close_http_client" else None)
    dispose_mock = AsyncMock(side_effect=RuntimeError("dispose boom") if failing == "engine.dispose" else None)
    _patch_shutdown(monkeypatch, close_mock, dispose_mock)

    with caplog.at_level(logging.WARNING, logger="app.server"), TestClient(server.create_app()):
        pass

    close_mock.assert_awaited_once()
    dispose_mock.assert_awaited_once()
    expected_log = "关闭共享 HTTP 客户端失败" if failing == "close_http_client" else "释放数据库连接失败"
    assert any(expected_log in record.getMessage() for record in caplog.records)


async def test_shutdown_runs_when_lifespan_body_raises(monkeypatch):
    """yield 点收到异常时（旧写法直接跳过关闭），try/finally 必须让两步都执行。"""
    close_mock = AsyncMock()
    dispose_mock = AsyncMock()
    monkeypatch.setattr(server, "cleanup_expired_access_tokens", AsyncMock(return_value=0))
    _patch_shutdown(monkeypatch, close_mock, dispose_mock)

    with pytest.raises(RuntimeError, match="上层异常"):
        async with server.lifespan(server.create_app()):
            raise RuntimeError("上层异常")

    close_mock.assert_awaited_once()
    dispose_mock.assert_awaited_once()
