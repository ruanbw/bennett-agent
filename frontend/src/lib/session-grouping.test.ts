import { describe, expect, it } from 'vitest';

import { groupSessionsByDate } from './session-grouping';
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

  it('缺少 updatedAt 时回落到 createdAt', () => {
    const groups = groupSessionsByDate(
      [{ ...session('legacy', 0), updatedAt: 0, createdAt: NOW } as SessionRecord],
      NOW,
    );
    expect(groups[0].label).toBe('今天');
  });
});
