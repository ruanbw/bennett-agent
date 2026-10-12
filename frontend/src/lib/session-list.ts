/**
 * 会话列表的纯逻辑：排序、重命名/置顶要写库的字段、多选与搜索过滤的交集。
 *
 * 放在 lib 而不是组件/hook 里：这几条规则最容易悄悄出错（一次无变化的保存把旧会话
 * 顶到最前、批量操作牵连被搜索隐藏的选中项），而 vitest 的 node 环境跑不了组件与
 * IndexedDB。规则本身抽成纯函数后就能直接回归。
 */

import type { SessionRecord } from '../types';

/** 排序：置顶在前；同组内按最近活动（updatedAt）倒序。不原地修改入参。 */
export function sortSessions(sessions: readonly SessionRecord[]): SessionRecord[] {
  return [...sessions].sort((a, b) => {
    const pinA = a.isPinned ? 1 : 0;
    const pinB = b.isPinned ? 1 : 0;
    if (pinA !== pinB) return pinB - pinA;
    return (b.updatedAt || 0) - (a.updatedAt || 0);
  });
}

/**
 * 重命名要写库的字段；标题没有任何变化时返回 null（调用方跳过写库）。
 *
 * 原实现无条件 bump updatedAt：点开重命名、原样保存也会把一条旧会话顶到列表最前。
 * 传 undefined 表示当前列表里找不到这条记录（例如列表还没加载完），此时无从比较，
 * 照写——静默丢弃重命名比多写一次更糟。
 */
export function titleChange(
  session: Pick<SessionRecord, 'title'> | undefined,
  nextTitle: string,
  now: number,
): { title: string; updatedAt: number } | null {
  if (session && session.title === nextTitle) return null;
  return { title: nextTitle, updatedAt: now };
}

/**
 * 置顶/取消置顶要写库的字段（单条与批量共用，避免两种入口语义漂移）。
 *
 * 刻意不 bump updatedAt：updatedAt 的含义是「最近活动」，改置顶不是活动。bump 会让
 * 「取消置顶」把一条旧会话顶到未置顶区最前、「置顶」又让它排在置顶区最前——顺序由
 * 点按钮的先后决定。不 bump 时排序稳定：置顶只换分组，组内仍按最近活动排。
 */
export function pinChange(pinned: boolean): { isPinned: number } {
  return { isPinned: pinned ? 1 : 0 };
}

/**
 * 选中集合与「当前可见（搜索过滤后）」集合的交集。
 *
 * 批量操作只作用于这个集合：搜索把某些已选会话藏起来时不能被顺带删除，
 * 确认弹窗里的数量也必须等于实际删除集合。
 */
export function visibleSelection(
  selectedIds: ReadonlySet<string>,
  visibleIds: readonly string[],
): Set<string> {
  const visible = new Set<string>();
  for (const id of visibleIds) {
    if (selectedIds.has(id)) visible.add(id);
  }
  return visible;
}
