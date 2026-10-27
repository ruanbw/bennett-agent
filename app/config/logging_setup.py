"""日志配置：统一的格式、级别与第三方噪音抑制。

为什么单独成模块：日志配置此前只有 `main.py` 里一行 `logging.basicConfig(level=INFO)`。
这带来三个具体问题，都只有在真实部署里才暴露：

1. **访问日志把每个静态请求都刷一行**（`GET /api/models 200`），SSE 长连接与健康检查
   也会刷；排查问题时真正重要的 WARNING 被淹掉。级别应当可配（`LOG_LEVEL`），
   access log 单独可关（`LOG_ACCESS=false`）。
2. **uvicorn 自己配了 handler**：它会在 `dictConfig` 里给 `uvicorn`/`uvicorn.error`/
   `uvicorn.access` 装上 handler 且 `propagate=False`，所以应用层设置的格式对它们无效，
   日志行会呈现两套格式。要统一就得显式改这三个 logger 的 handler/level。
3. **第三方库在 DEBUG 下极其吵闹**：httpx/openai 会逐条打印请求头与请求体（含密钥），
   `LOG_LEVEL=DEBUG` 时这是真实的信息泄露风险，不只是噪音。

这里只放纯函数与小工具，`main.py` 负责调用。`tests/test_logging.py` 锁住「级别解析」
与「不会把 DEBUG 泄漏给 httpx」这两条。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable

#: 应用日志格式：时间、级别、请求 ID、logger 名、消息。级别左对齐便于肉眼扫。
#:
#: `request_id` 由 `app/observability.py` 的 `RequestIdFilter` 注入（挂在根 handler 上），
#: 不在请求上下文里时是 `-`。放在 logger 名之前：调试时先按请求 ID grep 出一组行，
#: 再按 logger 名细分，比反过来更常用。
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
    "multipart",
)

_VALID_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")


def parse_log_level(raw: str | None, default: str = "INFO") -> str:
    """把 `LOG_LEVEL` 归一化成 logging 认识的名字。

    认不出的取值退回默认级别并保持沉默：日志级别配错不该让服务起不来（这是运维在
    救火时最不需要的第二种故障），但也不能把 DEBUG 静默当 INFO——那会让用户以为
    自己开的调试日志没生效。所以只接受标准级别名，大小写不敏感。
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


def log_level_from_env(env: dict[str, str] | None = None) -> str:
    """`LOG_LEVEL` 的取值（默认 INFO）。"""
    source = os.environ if env is None else env
    return parse_log_level(source.get("LOG_LEVEL"))


def access_log_enabled(env: dict[str, str] | None = None) -> bool:
    """是否输出访问日志。默认关闭：SSE 与前端轮询会让它变成噪音。"""
    source = os.environ if env is None else env
    return parse_bool_env(source.get("LOG_ACCESS"), default=False)


def quiet_third_party_loggers(level: int, names: Iterable[str] = NOISY_THIRD_PARTY) -> None:
    """把第三方库压到 WARNING（除非全局级别更高）。

    DEBUG 下 httpx 会打印完整请求头——里面就是 `Authorization: Bearer <key>`。
    这不是「日志多」，而是把凭据写进日志文件。
    """
    floor = max(level, logging.WARNING)
    for name in names:
        logging.getLogger(name).setLevel(floor)


def configure_logging(
    *,
    level: str | None = None,
    access_log: bool | None = None,
) -> int:
    """配置根 logger 与 uvicorn 的 logger，返回最终生效的级别。

    幂等：`basicConfig(force=True)` 会重建 handler，重复调用不会叠加输出。
    """
    resolved = level if level is not None else log_level_from_env()
    resolved = parse_log_level(resolved)
    numeric = logging.getLevelName(resolved)
    if not isinstance(numeric, int):  # 理论上不会发生；parse_log_level 已保证合法
        numeric = logging.INFO

    logging.basicConfig(level=numeric, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT, force=True)

    # 给根 handler 挂上 RequestIdFilter（`basicConfig` 刚建好它）。挂在 handler 上而不是
    # logger 上：handler 级 filter 对**所有**经过该 handler 的记录生效，包括从子 logger
    # 冒泡上来的——这正是我们要的（每条日志都带同一个 request id）。
    from app.observability import RequestIdFilter

    for handler in logging.getLogger().handlers:
        # 重复调用 configure_logging 时不叠加（幂等，与 basicConfig 的 force=True 对齐）
        if not any(isinstance(existing, RequestIdFilter) for existing in handler.filters):
            handler.addFilter(RequestIdFilter())

    # uvicorn 的三件套：改成同一套格式，并按需关掉 access log
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    want_access = access_log_enabled() if access_log is None else access_log
    for name in UVICORN_LOGGERS:
        uv_logger = logging.getLogger(name)
        for handler in list(uv_logger.handlers):
            uv_logger.removeHandler(handler)
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        # uvicorn 的 handler 也要带 request id，否则「同一条请求」的应用日志与 uvicorn
        # 访问日志格式不一致，混在一个文件里看不出对应关系。
        handler.addFilter(RequestIdFilter())
        uv_logger.addHandler(handler)
        # propagate=False：否则同一条记录既走 uvicorn 的 handler、又冒泡到根 logger，
        # 同一条日志打印两遍（`log_config=None` 之后尤甚）。
        uv_logger.propagate = False
        # 关掉访问日志时压到 WARNING：uvicorn 的 access 记录是 INFO，过滤掉即可
        uv_logger.setLevel(logging.WARNING if name == "uvicorn.access" and not want_access else numeric)

    quiet_third_party_loggers(numeric)
    return numeric
