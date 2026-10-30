"""日志配置：统一的格式、级别与第三方噪音抑制；这里只放纯函数，调用在 `main.py`。
uvicorn 的三个 logger 被 `propagate=False` 固定，必须逐个换 handler 才能与应用日志同格式；
httpx/openai 在 DEBUG 下会打印请求头（含 API Key），必须压到 WARNING。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable

#: 应用日志格式：时间、级别、请求 ID、logger 名、消息（级别左对齐便于扫）。
#: `request_id` 由 `app/observability.py` 的 `RequestIdFilter` 注入，不在请求上下文里时是 `-`。
LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: uvicorn 自己的三个 logger：它们在 `uvicorn.config` 里被 `propagate=False` 固定，
#: 必须逐个改 handler 才能与应用日志同格式。
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")

#: 即使在 DEBUG 下也要压到 WARNING 的第三方库：它们会把请求头/请求体打进日志，
#: 其中包含 Authorization 与 API Key。
NOISY_THIRD_PARTY: tuple[str, ...] = (
    "httpx",
    "httpcore",
    "openai",
    "urllib3",
    "asyncio",
)

_VALID_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")


def parse_log_level(raw: str | None, default: str = "INFO") -> str:
    """把 `LOG_LEVEL` 归一化成 logging 认识的名字。
    认不出的值退回默认：配错级别不该让服务起不来；但也只接受标准级别名，不能把 DEBUG 静默当 INFO。
    """
    candidate = (raw or "").strip().upper()
    return candidate if candidate in _VALID_LEVELS else default


def parse_bool_env(raw: str | None, default: bool) -> bool:
    """`true/1/yes/on` 视为真，`false/0/no/off` 视为假，其余退回默认。"""
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return default


#: `LogRecord` 自带的属性名。JSON 输出时把不属于这些的键当作调用方 `extra=` 传进来的业务字段。
_RESERVED_RECORD_KEYS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()) | {
    "message",
    "asctime",
    "request_id",
}


class JsonFormatter(logging.Formatter):
    """单行 JSON 日志。

    为什么需要：文本格式下 traceback 是多行，采集器按行切分会把栈帧与它所属的 `request_id`
    拆到不同事件里，事后无法把一次 500 的所有日志串起来。JSON 把栈整体塞进 `exc_info` 一个字段，
    一条记录永远只占一行。

    不做任何字段裁剪：调用方 `extra=` 传的业务字段（如 `user_id`、`conversation_id`）原样带上，
    否则要排查时还得回代码里找打了什么。不可序列化的值退化成 `str()`，不是为了好看，
    是因为日志格式化抛异常会把产生日志的那个请求一起打挂。
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": self.formatTime(record, LOG_DATE_FORMAT),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED_RECORD_KEYS:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        elif record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def make_formatter(*, json_logs: bool) -> logging.Formatter:
    """两种格式共用同一个入口，避免 JSON 开关只在根 handler 生效、uvicorn 的 handler 还是文本。"""
    if json_logs:
        return JsonFormatter(datefmt=LOG_DATE_FORMAT)
    return logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)


def json_logs_from_env(env: dict[str, str] | None = None) -> bool:
    """`LOG_JSON` 的取值（默认 text）。"""
    source = os.environ if env is None else env
    return parse_bool_env(source.get("LOG_JSON"), default=False)


def log_level_from_env(env: dict[str, str] | None = None) -> str:
    """`LOG_LEVEL` 的取值（默认 INFO）。"""
    source = os.environ if env is None else env
    return parse_log_level(source.get("LOG_LEVEL"))


def access_log_enabled(env: dict[str, str] | None = None) -> bool:
    """是否输出访问日志。默认关闭：SSE 与前端轮询会让它变成噪音。"""
    source = os.environ if env is None else env
    return parse_bool_env(source.get("LOG_ACCESS"), default=False)


def quiet_third_party_loggers(level: int, names: Iterable[str] = NOISY_THIRD_PARTY) -> None:
    """把第三方库压到 WARNING（除非全局级别更高）：DEBUG 下 httpx 会打印 `Authorization: Bearer <key>`。"""
    floor = max(level, logging.WARNING)
    for name in names:
        logging.getLogger(name).setLevel(floor)


def configure_logging(
    *,
    level: str | None = None,
    access_log: bool | None = None,
    json_logs: bool | None = None,
) -> int:
    """配置根 logger 与 uvicorn 的 logger，返回最终生效的级别；`basicConfig(force=True)` 保证幂等。"""
    resolved = level if level is not None else log_level_from_env()
    resolved = parse_log_level(resolved)
    numeric = logging.getLevelName(resolved)
    if not isinstance(numeric, int):  # 理论上不会发生；parse_log_level 已保证合法
        numeric = logging.INFO

    want_json = json_logs_from_env() if json_logs is None else json_logs
    logging.basicConfig(level=numeric, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT, force=True)
    if want_json:
        # basicConfig 只接受 format 字符串，换不了 formatter 类；只能事后把根 handler 的格式换掉。
        for handler in logging.getLogger().handlers:
            handler.setFormatter(make_formatter(json_logs=True))

    # 挂在 handler 而不是 logger 上：handler 级 filter 对所有经过它的记录生效，包括子 logger 冒泡上来的。
    from app.observability import RequestIdFilter

    for handler in logging.getLogger().handlers:
        # 重复调用不叠加（与 basicConfig 的 force=True 对齐）
        if not any(isinstance(existing, RequestIdFilter) for existing in handler.filters):
            handler.addFilter(RequestIdFilter())

    # uvicorn 的三件套：改成同一套格式，并按需关掉 access log
    formatter = make_formatter(json_logs=want_json)
    want_access = access_log_enabled() if access_log is None else access_log
    for name in UVICORN_LOGGERS:
        uv_logger = logging.getLogger(name)
        for handler in list(uv_logger.handlers):
            uv_logger.removeHandler(handler)
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        # 访问日志也要带 request id，否则同一条请求的应用日志与 uvicorn 日志对不上。
        handler.addFilter(RequestIdFilter())
        uv_logger.addHandler(handler)
        # propagate=False：否则同一条记录既走 uvicorn 的 handler 又冒泡到根 logger，会打印两遍。
        uv_logger.propagate = False
        # 关掉访问日志时压到 WARNING：uvicorn 的 access 记录是 INFO，过滤掉即可
        uv_logger.setLevel(logging.WARNING if name == "uvicorn.access" and not want_access else numeric)

    quiet_third_party_loggers(numeric)
    return numeric
