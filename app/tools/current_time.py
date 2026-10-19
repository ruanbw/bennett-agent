"""当前时间感知工具。

模型的知识有时间截止点，「今天几号」「现在几点」「这周还剩几天」它只能猜。做成工具
而不是写进 system prompt，原因是装配好的 Agent 按 `(model, effort, settings)` 缓存
复用（见 `app/services/agent_factory.py`）：提示词里的 `{{ now }}` 会冻结在缓存那一刻，
一个**悄悄过期**的时间比没有时间更糟——模型会理直气壮地答错，还带具体的数字。

工具本身是纯计算：无外部依赖、无参数、无可失败分支，因此始终挂载，也无需超时。
"""

from datetime import datetime, timedelta, timezone

from pydantic_ai import RunContext

#: 中文星期。`strftime("%A")` 给的是英文全称，模型还得自己翻译一遍，
#: 而「周几」正是它最容易算错的一类相对时间推理。
_WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

#: 时区不同时的提醒。服务端时区（容器里往往是 UTC）与用户所在时区经常不一致，
#: 直接照搬服务器时间会答出一个「日期对但日期感觉不对」的答案。
_TIMEZONE_HINT = "说明：以上为服务器本地时间；若用户所在时区不同，请按用户时区换算并注明。"


def _format_offset(offset: timedelta) -> str:
    """把 UTC 偏移格式化为 `UTC+08:00` 形式。

    分钟级偏移真实存在（印度 UTC+05:30、纽芬兰 UTC-03:30），只取小时会把它们
    悄悄改写成错误的时区。
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

    与取时间分离，测试才能固定时间而不依赖「测试跑的那一秒」。
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

    当问题涉及「今天」「现在」「今年」「最近」「还有几天」「本周」等与当前时间相关的
    内容时调用它取得真实时间；不要凭训练数据推测当前日期。
    """
    return format_current_time(datetime.now().astimezone())
