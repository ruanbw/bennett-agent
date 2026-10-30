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


def test_json_formatter_keeps_traceback_on_one_line():
    """JSON 日志存在的唯一理由：一条记录只占一行，栈帧不能与它的 request_id 分家。

    文本格式下 traceback 是多行，采集器按行切分会把栈帧切成独立事件，
    事后无法把一次 500 抛出的所有日志按 request_id 串起来。
    """
    import json

    from app.config.logging_setup import JsonFormatter

    record = logging.LogRecord("app.api.chat", logging.ERROR, __file__, 1, "boom %s", ("x",), None)
    record.request_id = "req-123"
    try:
        raise RuntimeError("inner")
    except RuntimeError:
        import sys

        record.exc_info = sys.exc_info()

    line = JsonFormatter().format(record)
    assert "\n" not in line, "多行输出会让按行切分的采集器把栈帧孤立出来"
    payload = json.loads(line)
    assert payload["request_id"] == "req-123"
    assert payload["level"] == "ERROR"
    assert "RuntimeError: inner" in payload["exc_info"]


def test_json_formatter_carries_extra_fields_and_survives_unserializable():
    """`extra=` 传的业务字段必须带出来；不可序列化的值退化成 str 而不是让格式化抛异常——
    否则产生日志的那个请求会跟着一起 500。"""
    import json

    from app.config.logging_setup import JsonFormatter

    record = logging.LogRecord("app.api.chat", logging.INFO, __file__, 1, "hi", None, None)
    record.user_id = "u-1"
    record.blob = object()

    payload = json.loads(JsonFormatter().format(record))
    assert payload["user_id"] == "u-1"
    assert isinstance(payload["blob"], str)


@pytest.mark.parametrize(("raw", "expected"), [("1", True), ("true", True), ("0", False), ("", False), (None, False)])
def test_json_logs_from_env(raw, expected):
    from app.config.logging_setup import json_logs_from_env

    env = {} if raw is None else {"LOG_JSON": raw}
    assert json_logs_from_env(env) is expected


def test_configure_logging_switches_both_root_and_uvicorn_to_json():
    """开关不能只作用于根 handler：uvicorn 的三个 logger 有独立 handler，
    漏掉它们的结果是同一份日志里一半 JSON 一半文本，采集器直接解析失败。"""
    import json

    from app.config.logging_setup import configure_logging

    try:
        configure_logging(level="INFO", access_log=False, json_logs=True)
        for name in (None, *UVICORN_LOGGERS):
            logger = logging.getLogger() if name is None else logging.getLogger(name)
            for handler in logger.handlers:
                line = handler.formatter.format(logging.LogRecord("x", logging.INFO, __file__, 1, "m", None, None))
                assert json.loads(line)["message"] == "m", f"{name or 'root'} 的 handler 仍是文本格式"
    finally:
        configure_logging(level="INFO", access_log=False, json_logs=False)
