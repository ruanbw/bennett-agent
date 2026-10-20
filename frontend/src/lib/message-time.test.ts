/**
 * 消息时间格式的行为锁。
 *
 * 「今天」按**本地日历日**判断而不是 24 小时差：凌晨看昨晚的消息必须显示日期，
 * 否则用户分不清「昨天 23:50」和「今天 00:20」。
 */

import { describe, expect, it } from 'vitest';

import { formatMessageTime, formatMessageTimeFull } from './message-time';

/** 本地时区的固定时刻，断言不依赖运行环境 TZ。 */
const at = (year: number, month: number, day: number, hour: number, minute: number) =>
  new Date(year, month - 1, day, hour, minute, 0);

const now = at(2026, 10, 21, 14, 30);

describe('formatMessageTime', () => {
  it('今天只显示时分', () => {
    expect(formatMessageTime(at(2026, 10, 21, 9, 5), now)).toBe('09:05');
    expect(formatMessageTime(at(2026, 10, 21, 14, 29), now)).toBe('14:29');
  });

  it('今天凌晨与昨晚 23:50 一眼可分', () => {
    const justAfterMidnight = at(2026, 10, 21, 0, 10);
    const lastNight = at(2026, 10, 20, 23, 50);

    expect(formatMessageTime(justAfterMidnight, now)).toBe('00:10');
    expect(formatMessageTime(lastNight, now)).toBe('10-20 23:50');
  });

  it('今年其它日期显示月日 + 时分', () => {
    expect(formatMessageTime(at(2026, 1, 2, 8, 0), now)).toBe('01-02 08:00');
  });

  it('跨年显示完整日期', () => {
    expect(formatMessageTime(at(2025, 12, 31, 23, 59), now)).toBe('2025-12-31 23:59');
  });

  it('非法时间返回空串（调用方据此不渲染时间）', () => {
    expect(formatMessageTime(null, now)).toBe('');
    expect(formatMessageTime(undefined, now)).toBe('');
    expect(formatMessageTime(new Date(Number.NaN), now)).toBe('');
  });
});

describe('formatMessageTimeFull', () => {
  it('精确到秒，作为悬浮提示', () => {
    expect(formatMessageTimeFull(new Date(2026, 9, 21, 9, 5, 7))).toBe('2026-10-21 09:05:07');
  });

  it('非法时间返回空串', () => {
    expect(formatMessageTimeFull(null)).toBe('');
  });
});
