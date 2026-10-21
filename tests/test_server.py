"""反代部署下的认证限流退化告警（`app.server`）。

`request.client.host` 在反代后面恒为代理地址，于是认证限流从「每 IP 10 次/分钟」
退化成「全站共享 10 次/分钟」。代码侧判断不出部署形态，只能在启动日志里说清楚。
"""

import logging

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
