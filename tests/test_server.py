"""反代部署下的认证限流退化告警（`app.server`）。

`request.client.host` 在反代后面恒为代理地址，于是认证限流从「每 IP 10 次/分钟」
退化成「全站共享 10 次/分钟」。代码侧判断不出部署形态，只能在启动日志里说清楚。
"""

import logging
from importlib.metadata import PackageNotFoundError

from app import server
from app.config import settings


def test_warns_when_auth_rate_limit_is_global(monkeypatch, caplog):
    monkeypatch.setattr(settings, "auth_rate_limit_per_minute", 10)
    monkeypatch.setattr(settings, "trusted_proxy_headers", "")

    with caplog.at_level(logging.WARNING, logger="app.server"):
        server._warn_if_auth_rate_limit_is_global()

    warning = " ".join(record.getMessage() for record in caplog.records)
    assert "TRUSTED_PROXY_HEADERS" in warning
    assert "10" in warning


def test_no_warning_when_trusted_proxy_headers_configured(monkeypatch, caplog):
    """配了受信头就说明知道自己在反代后面，不必再提醒。"""
    monkeypatch.setattr(settings, "auth_rate_limit_per_minute", 10)
    monkeypatch.setattr(settings, "trusted_proxy_headers", "X-Forwarded-For")

    with caplog.at_level(logging.WARNING, logger="app.server"):
        server._warn_if_auth_rate_limit_is_global()

    assert not [record for record in caplog.records if "TRUSTED_PROXY_HEADERS" in record.getMessage()]


def test_no_warning_when_auth_rate_limit_disabled(monkeypatch, caplog):
    """限流关掉时不存在「退化成单桶」这回事。"""
    monkeypatch.setattr(settings, "auth_rate_limit_per_minute", 0)
    monkeypatch.setattr(settings, "trusted_proxy_headers", "")

    with caplog.at_level(logging.WARNING, logger="app.server"):
        server._warn_if_auth_rate_limit_is_global()

    assert not caplog.records


def test_openapi_tags_are_documented_and_cover_every_operation():
    """每个端点都必须落在已声明的 tag 下，否则 /docs 会多出一个没有说明的 default 组。"""
    spec = server.create_app().openapi()

    declared = {tag["name"]: tag for tag in spec.get("tags", [])}
    assert {"auth", "chat", "capabilities", "sync", "admin"} <= set(declared)
    assert all(tag.get("description") for tag in declared.values())

    operations = 0
    for path, methods in spec["paths"].items():
        for method, operation in methods.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            operations += 1
            assert operation.get("tags"), f"{method.upper()} {path} 未分组"
            assert set(operation["tags"]) <= set(declared), f"{method.upper()} {path} 用了未声明的 tag"

    # 防止 spec 结构变化让上面的循环空转（路径一个都没遍历到也算「通过」）
    assert operations > 0


def test_openapi_description_explains_auth_and_docs_exposure():
    spec = server.create_app().openapi()

    description = spec["info"]["description"]
    assert "HttpOnly cookie" in description
    assert "ENABLE_API_DOCS" in description


def test_every_operation_has_a_chinese_summary():
    """框架自动生成的路由曾带着 `Auth:Cookie.Login` 这类英文 summary 混进 /docs。

    其余端点全是中文，只有那三条是英文，看起来像漏翻；而且它们连 description 都没有，
    打开 /docs 的人看不到「登录要发表单编码而不是 JSON」这种关键差异。
    """
    spec = server.create_app().openapi()

    missing = []
    for path, methods in spec["paths"].items():
        for method, operation in methods.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            summary = operation.get("summary") or ""
            if not any("\u4e00" <= char <= "\u9fff" for char in summary):
                missing.append(f"{method.upper()} {path}: {summary!r}")

    assert not missing, f"这些端点没有中文说明：{missing}"


def test_generated_auth_routes_document_their_contract():
    """登录是表单编码、注册不能指定角色——这两条差异必须写在文档里，而不是靠读代码。"""
    spec = server.create_app().openapi()

    login = spec["paths"]["/api/auth/login"]["post"]
    assert "表单编码" in login["description"]
    assert "username" in login["description"]

    register = spec["paths"]["/api/auth/register"]["post"]
    assert "普通用户" in register["description"]
    assert "没有任何请求参数能指定角色" in register["description"]

    logout = spec["paths"]["/api/auth/logout"]["post"]
    assert "失效" in logout["description"]


# ---------------------------------------------------------------------
# OpenAPI：版本只有单一来源、会话是 cookie 要在文档里声明
# ---------------------------------------------------------------------
def test_openapi_version_matches_the_package_version():
    """此前写死 `1.0.0`，与 pyproject 的 `0.1.0` 各说各话——版本是诊断信息，只能有一个来源。"""
    from app.version import APP_VERSION

    spec = server.create_app().openapi()

    assert spec["info"]["version"] == APP_VERSION
    assert spec["info"]["version"] != "1.0.0"


def test_version_falls_back_to_pyproject_when_metadata_is_missing(monkeypatch):
    """源码树直跑时没有包元数据：退回 pyproject 的 [project].version，而不是 0.0.0+unknown。"""
    from app import version as version_module

    monkeypatch.setattr(
        version_module,
        "version",
        lambda _name: (_ for _ in ()).throw(PackageNotFoundError("not installed")),
    )

    assert version_module.get_app_version() == version_module._version_from_pyproject()
    assert version_module.get_app_version() != version_module.UNKNOWN_VERSION


def test_version_is_unknown_only_when_no_source_exists(tmp_path, monkeypatch):
    from app import version as version_module

    monkeypatch.setattr(
        version_module,
        "version",
        lambda _name: (_ for _ in ()).throw(PackageNotFoundError("not installed")),
    )

    assert version_module._version_from_pyproject(tmp_path / "missing.toml") is None


def test_openapi_declares_the_cookie_session_scheme():
    """文档要能一眼看出「凭据是一个 cookie」：否则 /docs 里没有 Authorize 入口，客户端生成器
    也不知道把凭据放哪。"""
    from app.config import settings

    spec = server.create_app().openapi()
    schemes = spec["components"]["securitySchemes"]

    # 复用 fastapi-users 已声明的方案，不能再造一个等价的
    assert list(schemes) == ["APIKeyCookie"]
    scheme = schemes["APIKeyCookie"]
    assert scheme["type"] == "apiKey"
    assert scheme["in"] == "cookie"
    assert scheme["name"] == settings.auth_cookie_name
    assert "HttpOnly" in scheme["description"]


def test_openapi_marks_session_endpoints_but_not_the_public_ones():
    """公开端点若被标上认证要求，客户端生成器会在登录前就伸手要凭据（死循环）。"""
    spec = server.create_app().openapi()

    assert spec["paths"]["/api/chat"]["post"]["security"] == [{"APIKeyCookie": []}]
    assert spec["paths"]["/api/models"]["get"]["security"] == [{"APIKeyCookie": []}]
    assert spec["paths"]["/api/sync/pull"]["get"]["security"] == [{"APIKeyCookie": []}]
    assert spec["paths"]["/api/auth/login"]["post"].get("security") is None
    assert spec["paths"]["/api/auth/register"]["post"].get("security") is None


def test_every_operation_outside_the_public_list_requires_the_cookie():
    """新增一个公开端点必须显式改清单，而不是悄悄多出一个没有认证说明的接口。"""
    spec = server.create_app().openapi()

    missing = []
    for path, methods in spec["paths"].items():
        if path in server._PUBLIC_PATHS:
            continue
        for method, operation in methods.items():
            if method.lower() not in server._HTTP_METHODS:
                continue
            if operation.get("security") != [{"APIKeyCookie": []}]:
                missing.append(f"{method.upper()} {path}")

    assert not missing, f"这些端点没写认证要求：{missing}"


def test_public_path_list_does_not_contain_ghost_endpoints():
    """清单里写一个不存在的路径等于给后来者挖坑：它永远不会被校验到。"""
    spec = server.create_app().openapi()

    ghosts = sorted(path for path in server._PUBLIC_PATHS if path not in spec["paths"])
    assert not ghosts, f"公开端点清单里有不存在的路径：{ghosts}"
