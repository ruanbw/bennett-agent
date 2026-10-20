import type { SessionRecord } from '../types';

export interface SessionGroup {
  label: string;
  sessions: SessionRecord[];
}

/**
 * 会话列表按「最近活动」分组：置顶 / 今天 / 昨天 / 7 天内 / 30 天内 / 更早。
 *
 * 边界按**本地日历日**算，而不是从今天零点往前减 24 小时的整数倍：夏令时切换日
 * 只有 23 或 25 小时，减固定毫秒会把「昨天 00:30」误分到「7 天内」，也会让
 * 「7 天前」的边界漂移一小时。`new Date(y, m, d - n)` 走的是日历日运算，跨月、
 * 跨年、DST 都由运行时处理。
 *
 * 组内按最近活动倒序：列表通常已经由 `sortSessions` 排好，但同步拉回的增量、批量
 * 取消置顶等路径不保证这一点；分组只做划分而不再排序，会让「置顶」组里的顺序
 * 变成数据到达顺序。这里自己排一次，不依赖调用方的隐含约定。
 */

/** 会话的「最近活动」时间；老记录没有 updatedAt 时回落到 createdAt。 */
export function sessionActivityTime(session: Pick<SessionRecord, 'updatedAt' | 'createdAt'>): number {
  return session.updatedAt || session.createdAt || 0;
}

/** 本地日历日的零点。 */
function startOfLocalDay(time: number): number {
  const date = new Date(time);
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

/** 以本地日历日为单位偏移（n 可为负）；跨月 / 跨年 / DST 交给 Date 处理。 */
function shiftLocalDays(dayStart: number, days: number): number {
  const date = new Date(dayStart);
  return new Date(date.getFullYear(), date.getMonth(), date.getDate() + days).getTime();
}

/** 组内按最近活动倒序；不原地修改入参。 */
function byRecentActivity(a: SessionRecord, b: SessionRecord): number {
  return sessionActivityTime(b) - sessionActivityTime(a);
}

/**
 * 距离下一个本地午夜的毫秒数，至少 1 秒。
 *
 * 单独导出给 `useMidnightRefresh`：分组标签依赖「今天是哪一天」，标签页挂机过午夜后
 * 必须重新分组，否则第二天打开仍会看到昨天创建的会话挂在「今天」下面。
 */
export function msUntilNextLocalMidnight(now: number = Date.now()): number {
  const date = new Date(now);
  const nextMidnight = new Date(date.getFullYear(), date.getMonth(), date.getDate() + 1).getTime();
  return Math.max(nextMidnight - now, 1000);
}

/**
 * 将会话列表按时间维度分组。
 *
 * `7 天内` 覆盖「前天到 7 天前」（含第 7 天零点），`30 天内` 覆盖「8 天前到 30 天前」，
 * 因此每个标签描述的都是「距今多少天内」，而不是从今天零点减固定小时数后的近似。
 */
export function groupSessionsByDate(sessions: SessionRecord[], now: number = Date.now()): SessionGroup[] {
  if (sessions.length === 0) return [];

  const startOfToday = startOfLocalDay(now);
  const startOfYesterday = shiftLocalDays(startOfToday, -1);
  const sevenDaysAgo = shiftLocalDays(startOfToday, -7);
  const thirtyDaysAgo = shiftLocalDays(startOfToday, -30);

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

    const time = sessionActivityTime(session);
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
  const add = (label: string, list: SessionRecord[]) => {
    if (list.length > 0) groups.push({ label, sessions: [...list].sort(byRecentActivity) });
  };

  add('置顶对话', pinned);
  add('今天', today);
  add('昨天', yesterday);
  add('7 天内', last7Days);
  add('30 天内', last30Days);
  add('更早', older);

  // 兜底：理论上不会走到（每条会话都会落进上面某一组），保留以防边界计算将来被改坏
  if (groups.length === 0) {
    groups.push({ label: '全部对话', sessions: [...sessions].sort(byRecentActivity) });
  }

  return groups;
}
