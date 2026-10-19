import type { SessionRecord } from '../types';

export interface SessionGroup {
  label: string;
  sessions: SessionRecord[];
}

/**
 * 将会话列表按时间维度进行分组（今天 / 昨天 / 7 天内 / 30 天内 / 更早）。
 * 置顶会话可优先展示在置顶组中。
 */
export function groupSessionsByDate(sessions: SessionRecord[], now: number = Date.now()): SessionGroup[] {
  if (sessions.length === 0) return [];

  const nowDate = new Date(now);
  const startOfToday = new Date(nowDate.getFullYear(), nowDate.getMonth(), nowDate.getDate()).getTime();
  const startOfYesterday = startOfToday - 24 * 60 * 60 * 1000;
  const sevenDaysAgo = startOfToday - 6 * 24 * 60 * 60 * 1000;
  const thirtyDaysAgo = startOfToday - 29 * 24 * 60 * 60 * 1000;

  const pinned: SessionRecord[] = [];
  const today: SessionRecord[] = [];
  const yesterday: SessionRecord[] = [];
  const last7Days: SessionRecord[] = [];
  const last30Days: SessionRecord[] = [];
  const older: SessionRecord[] = [];

  for (const session of sessions) {
    if (session.isPinned) {
      pinned.push(session);
      continue;
    }

    const time = session.updatedAt || session.createdAt || 0;
    if (time >= startOfToday) {
      today.push(session);
    } else if (time >= startOfYesterday) {
      yesterday.push(session);
    } else if (time >= sevenDaysAgo) {
      last7Days.push(session);
    } else if (time >= thirtyDaysAgo) {
      last30Days.push(session);
    } else {
      older.push(session);
    }
  }

  const groups: SessionGroup[] = [];

  if (pinned.length > 0) {
    groups.push({ label: '置顶对话', sessions: pinned });
  }
  if (today.length > 0) {
    groups.push({ label: '今天', sessions: today });
  }
  if (yesterday.length > 0) {
    groups.push({ label: '昨天', sessions: yesterday });
  }
  if (last7Days.length > 0) {
    groups.push({ label: '7 天内', sessions: last7Days });
  }
  if (last30Days.length > 0) {
    groups.push({ label: '30 天内', sessions: last30Days });
  }
  if (older.length > 0) {
    groups.push({ label: '更早', sessions: older });
  }

  // 兜底：如果全部为 0 且无有效分组，直接归入「历史对话」
  if (groups.length === 0 && sessions.length > 0) {
    groups.push({ label: '全部对话', sessions });
  }

  return groups;
}
