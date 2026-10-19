"""当前时间感知工具。

这个工具很简单，容易被当成「不会坏的东西」跳过。真正会坏的是三件事：

1. **时区**：容器里通常是 UTC，直接照搬服务器时间会给用户一个「日期对但日期感觉不对」
   的答案，所以输出必须带明确偏移与 UTC 对照；
2. **星期**：`strftime("%A")` 给英文，模型还得翻译，而「周几」正是相对时间推理最容易
   错的一步；
3. **分钟级偏移**：印度 +05:30、纽芬兰 -03:30，只取小时会把时区悄悄改成错的。

另外锁住装配约定：它必须**始终可用**（纯本地计算，没有依赖），否则「今天几号」只能猜。
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.tools import current_time_tool, get_current_time
from app.tools.current_time import _format_offset, format_current_time
from app.tools.registry import RiskLevel, ToolCategory

#: 固定时间，避免用例结果随「跑测试的那一秒」变化。2024-01-07 是周日。
SUNDAY = datetime(2024, 1, 7, 9, 5, 6, tzinfo=timezone(timedelta(hours=8)))


def test_formats_date_weekday_and_timezone():
    text = format_current_time(SUNDAY)

    assert "2024-01-07 09:05:06" in text
    assert "周日" in text
    assert "UTC+08:00" in text


def test_utc_line_is_the_same_instant():
    """UTC 对照行必须换算而不是照抄：差 8 小时就写成差 8 小时，并跨日。"""
    text = format_current_time(SUNDAY)

    assert "2024-01-07T01:05:06Z" in text


def test_unix_timestamp_matches_the_instant():
    text = format_current_time(SUNDAY)

    assert f"Unix 时间戳：{int(SUNDAY.timestamp())}" in text


def test_weekday_names_cover_the_whole_week():
    """周一..周日逐日覆盖。写错一个下标不会被任何真实用例注意到，只会答错星期。"""
    names = []
    for day in range(1, 8):
        moment = datetime(2024, 1, day, 12, 0, 0, tzinfo=timezone.utc)  # 2024-01-01 是周一
        names.append(format_current_time(moment).split("（")[1].split("）")[0])

    assert names == ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (timedelta(hours=8), "UTC+08:00"),
        (timedelta(0), "UTC+00:00"),
        (timedelta(hours=-5), "UTC-05:00"),
        # 分钟级偏移：只取小时会变成 UTC+05:00 / UTC-03:00，把用户放到隔壁时区
        (timedelta(hours=5, minutes=30), "UTC+05:30"),
        (timedelta(hours=-3, minutes=-30), "UTC-03:30"),
        (timedelta(hours=-9, minutes=-30), "UTC-09:30"),
    ],
)
def test_offset_keeps_minutes_and_sign(offset, expected):
    assert _format_offset(offset) == expected


def test_naive_datetime_is_treated_as_local():
    """naive 时间按本地时区解释——`datetime.now()` 就是这个形状，不能当成 UTC。"""
    naive = datetime(2024, 1, 7, 9, 5, 6)

    assert format_current_time(naive) == format_current_time(naive.astimezone())


def test_mentions_server_timezone_limits():
    """服务端时区常与用户不同，输出里要有这层提醒，模型才知道该换算。"""
    assert "时区不同" in format_current_time(SUNDAY)


@pytest.mark.asyncio
async def test_tool_returns_current_time_without_arguments():
    text = await get_current_time(None)

    today = datetime.now().astimezone().date().isoformat()
    assert "当前时间" in text
    assert today in text


@pytest.mark.asyncio
async def test_managed_tool_is_directly_callable():
    text = await current_time_tool(None)

    assert "当前时间" in text


def test_tool_is_always_enabled_and_read_only():
    """纯本地计算：无依赖、无副作用、无超时需求。"""
    assert current_time_tool.category == ToolCategory.SYSTEM
    assert current_time_tool.risk_level == RiskLevel.READ_ONLY
    assert current_time_tool.timeout is None
    assert current_time_tool.is_enabled
