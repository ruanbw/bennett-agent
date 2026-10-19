"""备用模型切换收集器的行为锁。

收集器是「模型内部切换」与「对话流事件」之间唯一的接缝：没有它，切换对用户完全
不可见；它若在 ContextVar 里留下脏状态，下一次请求会误报切换。
"""

from app.services.fallback_tracker import collect_fallbacks, record_fallback


def test_records_switches_in_order():
    with collect_fallbacks() as occurrences:
        record_fallback("primary", "fallback-1")
        record_fallback("primary", "fallback-2")

    assert [(item.primary_model, item.fallback_model) for item in occurrences] == [
        ("primary", "fallback-1"),
        ("primary", "fallback-2"),
    ]


def test_recording_outside_a_collector_is_ignored():
    """CLI 直接跑 Agent、标题生成等路径没有开启收集器，不能因此报错。"""
    record_fallback("primary", "fallback")

    with collect_fallbacks() as occurrences:
        assert occurrences == []
        record_fallback("primary", "fallback")
        assert len(occurrences) == 1


def test_collector_is_reset_after_the_context_exits():
    with collect_fallbacks() as occurrences:
        record_fallback("primary", "fallback")

    record_fallback("primary", "fallback-after-exit")

    assert len(occurrences) == 1
