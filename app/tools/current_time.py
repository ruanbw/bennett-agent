"""当前时间感知工具。

不能写进 system prompt：Agent 带缓存复用（见 `app/services/agent_factory.py`），提示词里的时间会冻结在
缓存那一刻，悄悄过期比没有时间更糟；工具本身纯计算、无参数、无可失败分支，始终挂载、无需超时。
"""

from datetime import datetime, timedelta, timezone

from pydantic_ai import RunContext

#: 中文星期：`strftime("%A")` 给英文全称，模型还得自己翻译，而「周几」正是它最容易算错的一类推理。
_WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

#: 时区提醒：容器里的服务端时间常是 UTC，直接照搬会答出「日期对但感觉不对」的答案。
_TIMEZONE_HINT = "说明：以上为服务器本地时间；若用户所在时区不同，请按用户时区换算并注明。"


def _format_offset(offset: timedelta) -> str:
    """把 UTC 偏移格式化为 `UTC+08:00` 形式。

    分钟级偏移真实存在（印度 UTC+05:30），只取小时会把它们悄悄改写成错误的时区。
    """
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    hours, minutes = divmod(abs(total_minutes), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def _normalize(now: datetime) -> datetime:
    """补齐时区。naive 时间按本地时区解释（这正是 `astimezone()` 的语义）。"""
    return now.astimezone() if now.tzinfo is None else now


def format_current_time(now: datetime) -> str:
    """把时间对象格式化成给模型看的文本。

    与取时间分离，测试才能固定时间。
    """
    local = _normalize(now)
    utc = local.astimezone(timezone.utc)
    zone_name = local.tzname()
    zone_label = _format_offset(local.utcoffset() or timedelta(0))
    if zone_name:
        zone_label = f"{zone_label}（{zone_name}）"

    return "\n".join(
        [
            f"当前时间：{local:%Y-%m-%d %H:%M:%S}（{_WEEKDAY_NAMES[local.weekday()]}）",
            f"时区：{zone_label}",
            f"UTC 时间：{utc:%Y-%m-%dT%H:%M:%S}Z",
            f"Unix 时间戳：{int(local.timestamp())}",
            _TIMEZONE_HINT,
        ]
    )


async def get_current_time(ctx: RunContext[None]) -> str:
    """获取当前的日期、时间与星期，附带时区与 UTC 时间。

    问题涉及「今天」「现在」「最近」「还有几天」等相对时间时用它取真实时间，不要凭训练数据推测。
    """
    return format_current_time(datetime.now().astimezone())
