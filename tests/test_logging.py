"""日志配置（`app/config/logging_setup.py`）。

三条行为必须锁住：级别解析不能把配错的取值静默当 DEBUG（等于把凭据打进日志），
DEBUG 下第三方库仍要压在 WARNING（httpx 会打印含 Authorization 的请求头），
访问日志默认关闭且可显式打开。
"""

import logging

import pytest

from app.config.logging_setup import (
    NOISY_THIRD_PARTY,
    UVICORN_LOGGERS,
    access_log_enabled,
    configure_logging,
    log_level_from_env,
    parse_bool_env,
    parse_log_level,
    quiet_third_party_loggers,
)


@pytest.fixture(autouse=True)
def _restore_logging():
    """每个用例都会改全局 logger：跑完恢复默认级别，避免影响其他测试。"""
    root = logging.getLogger()
    original = root.level
    yield
    root.setLevel(original)
    for name in (*UVICORN_LOGGERS, *NOISY_THIRD_PARTY):
        logging.getLogger(name).setLevel(logging.NOTSET)


def test_parse_log_level_accepts_standard_names_case_insensitively():
    assert parse_log_level("debug") == "DEBUG"
    assert parse_log_level(" Warning ") == "WARNING"
    assert parse_log_level("INFO") == "INFO"


@pytest.mark.parametrize("raw", ["", None, "verbose", "10", "警告"])
def test_parse_log_level_falls_back_to_info(raw):
    """配错的取值退回 INFO，不能静默变成 DEBUG（DEBUG 会把请求头写进日志）。"""
    assert parse_log_level(raw) == "INFO"


def test_parse_bool_env_accepts_common_spellings():
    for raw in ("1", "true", "YES", " on "):
        assert parse_bool_env(raw, default=False) is True
    for raw in ("0", "false", "No", "off"):
        assert parse_bool_env(raw, default=True) is False
    assert parse_bool_env(None, default=True) is True
    assert parse_bool_env("maybe", default=False) is False


def test_access_log_is_off_by_default_and_can_be_enabled():
    assert access_log_enabled({}) is False
    assert access_log_enabled({"LOG_ACCESS": "true"}) is True
    assert access_log_enabled({"LOG_ACCESS": "false"}) is False


def test_debug_keeps_third_party_at_warning():
    """DEBUG 下 httpx/openai 会打印请求头（含 Authorization），必须压到 WARNING。"""
    quiet_third_party_loggers(logging.DEBUG)

    for name in ("httpx", "httpcore", "openai"):
        assert logging.getLogger(name).level == logging.WARNING


def test_configure_logging_sets_root_level_and_quiets_uvicorn_access():
    numeric = configure_logging(level="DEBUG", access_log=False)

    assert numeric == logging.DEBUG
    assert logging.getLogger().level == logging.DEBUG
    assert logging.getLogger("uvicorn.access").level == logging.WARNING
    # 非 access 的 uvicorn logger 跟随全局级别
    assert logging.getLogger("uvicorn.error").level == logging.DEBUG


def test_configure_logging_can_turn_access_log_on():
    configure_logging(level="INFO", access_log=True)

    assert logging.getLogger("uvicorn.access").level == logging.INFO


def test_uvicorn_loggers_do_not_propagate():
    """同一条记录既走 uvicorn 的 handler 又冒泡到根 logger，会打印两遍。"""
    configure_logging(level="INFO", access_log=False)

    for name in UVICORN_LOGGERS:
        assert logging.getLogger(name).propagate is False


def test_configure_logging_is_idempotent():
    """重复调用不该叠加 handler（否则每条日志会打印多遍）。"""
    configure_logging(level="INFO", access_log=False)
    configure_logging(level="INFO", access_log=False)

    assert len(logging.getLogger("uvicorn.error").handlers) == 1


def test_log_level_can_come_from_the_environment():
    """不传参数时读 LOG_LEVEL：部署侧不需要改代码就能调级别。"""
    assert log_level_from_env({"LOG_LEVEL": "warning"}) == "WARNING"
    assert log_level_from_env({}) == "INFO"
    assert log_level_from_env({"LOG_LEVEL": "loud"}) == "INFO"
