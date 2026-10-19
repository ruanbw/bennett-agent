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
