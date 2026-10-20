import { describe, expect, it } from 'vitest';

import {
  groupSessionsByDate,
  msUntilNextLocalMidnight,
  sessionActivityTime,
} from './session-grouping';
import type { SessionRecord } from '../types';

/** 造一条最小可用的会话记录（只填分组逻辑真正读到的字段）。 */
function session(id: string, updatedAt: number, overrides: Partial<SessionRecord> = {}): SessionRecord {
  return {
    id,
    title: `会话 ${id}`,
    createdAt: updatedAt,
    updatedAt,
    isPinned: 0,
    ...overrides,
  } as SessionRecord;
}

// 固定「现在」= 2026-10-19 12:00 本地时间，避免用例随真实日期漂移
const NOW = new Date(2026, 9, 19, 12, 0, 0).getTime();
const DAY = 24 * 60 * 60 * 1000;

/** 本地时间构造，断言不依赖运行环境 TZ（与 `message-time.test.ts` 同一做法）。 */
const at = (month: number, day: number, hour = 0, minute = 0, second = 0, ms = 0): number =>
  new Date(2026, month - 1, day, hour, minute, second, ms).getTime();

describe('groupSessionsByDate', () => {
  it('空列表返回空分组', () => {
    expect(groupSessionsByDate([], NOW)).toEqual([]);
  });

  it('按今天 / 昨天 / 7 天内 / 30 天内 / 更早分段', () => {
    const groups = groupSessionsByDate(
      [
        session('today', NOW - 60 * 60 * 1000),
        session('yesterday', NOW - DAY),
        session('within7', NOW - 4 * DAY),
        session('within30', NOW - 20 * DAY),
        session('older', NOW - 90 * DAY),
      ],
      NOW,
    );

    expect(groups.map((group) => group.label)).toEqual([
      '今天',
      '昨天',
      '7 天内',
      '30 天内',
      '更早',
    ]);
    expect(groups.map((group) => group.sessions.map((s) => s.id))).toEqual([
      ['today'],
      ['yesterday'],
      ['within7'],
      ['within30'],
      ['older'],
    ]);
  });

  it('置顶会话单独成组并排在最前', () => {
    const groups = groupSessionsByDate(
      [session('today', NOW), session('pinned', NOW - 100 * DAY, { isPinned: 1 })],
      NOW,
    );

    expect(groups[0].label).toBe('置顶对话');
    expect(groups[0].sessions.map((s) => s.id)).toEqual(['pinned']);
    expect(groups[1].label).toBe('今天');
  });

  it('空分组不出现（不渲染只有标题没有内容的段）', () => {
    const groups = groupSessionsByDate([session('today', NOW)], NOW);
    expect(groups).toHaveLength(1);
    expect(groups[0].label).toBe('今天');
  });

  it('边界按本地日历日：今天零点前最后一毫秒属于昨天', () => {
    const todayStart = at(10, 19);
    const groups = groupSessionsByDate(
      [session('last-ms', todayStart - 1), session('first-ms', todayStart)],
      NOW,
    );

    expect(groups.map((group) => [group.label, group.sessions.map((s) => s.id)])).toEqual([
      ['今天', ['first-ms']],
      ['昨天', ['last-ms']],
    ]);
  });

  it('7 天 / 30 天边界也是日历日：第 7 天与第 30 天的零点仍算「以内」', () => {
    const groups = groupSessionsByDate(
      [
        session('seven', at(10, 12)),
        session('eight', at(10, 11, 23, 59, 59, 999)),
        session('thirty', at(9, 19)),
        session('older', at(9, 18, 23, 59, 59, 999)),
      ],
      NOW,
    );

    expect(groups.map((group) => [group.label, group.sessions.map((s) => s.id)])).toEqual([
      ['7 天内', ['seven']],
      ['30 天内', ['eight', 'thirty']],
      ['更早', ['older']],
    ]);
  });

  it('乱序输入在组内按最近活动倒序（不依赖调用方预排序）', () => {
    const groups = groupSessionsByDate(
      [session('a', NOW - 3 * 60 * 60 * 1000), session('b', NOW - 60 * 60 * 1000), session('c', NOW - 2 * 60 * 60 * 1000)],
      NOW,
    );

    expect(groups[0].sessions.map((s) => s.id)).toEqual(['b', 'c', 'a']);
  });

  it('置顶组同样按最近活动倒序（取消置顶不会把旧会话顶到最前）', () => {
    const groups = groupSessionsByDate(
      [
        session('old-pin', NOW - 50 * DAY, { isPinned: 1 }),
        session('new-pin', NOW - 10 * DAY, { isPinned: 1 }),
        session('today', NOW),
      ],
      NOW,
    );

    expect(groups[0].label).toBe('置顶对话');
    expect(groups[0].sessions.map((s) => s.id)).toEqual(['new-pin', 'old-pin']);
  });

  it('缺少 updatedAt 时回落到 createdAt', () => {
    const groups = groupSessionsByDate(
      [{ ...session('legacy', 0), updatedAt: 0, createdAt: NOW } as SessionRecord],
      NOW,
    );
    expect(groups[0].label).toBe('今天');
  });
});

describe('sessionActivityTime', () => {
  it('优先 recent activity；updatedAt 为 0 时回落到 createdAt', () => {
    expect(sessionActivityTime({ updatedAt: 200, createdAt: 100 })).toBe(200);
    expect(sessionActivityTime({ updatedAt: 0, createdAt: 100 })).toBe(100);
    expect(sessionActivityTime({ updatedAt: 0, createdAt: 0 })).toBe(0);
  });
});

describe('msUntilNextLocalMidnight', () => {
  it('从 23:59:30 到下一个本地午夜正好 30 秒', () => {
    expect(msUntilNextLocalMidnight(at(10, 19, 23, 59, 30))).toBe(30_000);
  });

  it('恰好午夜时返回「到第二天午夜」的日历差（不写死 24 小时）', () => {
    const now = at(10, 19);
    const nextMidnight = at(10, 20);

    expect(msUntilNextLocalMidnight(now)).toBe(nextMidnight - now);
  });

  it('跨月正确：10 月最后一晚的下一个午夜是 11 月 1 日零点', () => {
    const now = at(10, 31, 23, 0);
    const nextMidnight = at(11, 1);

    expect(msUntilNextLocalMidnight(now)).toBe(nextMidnight - now);
  });

  it('下限 1 秒，避免时钟异常时 0 毫秒定时器忙等', () => {
    expect(msUntilNextLocalMidnight(NOW)).toBeGreaterThanOrEqual(1000);
  });
});
