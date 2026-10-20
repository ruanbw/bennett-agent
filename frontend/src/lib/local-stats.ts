/**
 * 本机数据统计：设置面板里的「本机数据」区块。
 *
 * 这个应用的数据默认只存在本机 IndexedDB 里（服务端同步是补强），用户应该有地方看到
 * 「我这里到底存了多少东西、占了多少空间」——尤其是存储被浏览器清掉、或者配额告急时。
 *
 * 两个口径：
 * - 会话数与消息数**只算真实数据**：墓碑会话（`deleted: true`）是同步用的删除标记，
 *   算进去会让「3 个会话」和侧边栏列表对不上。
 * - 占用字节数来自 `navigator.storage.estimate()`，是**整个 origin** 的估算值
 *   （包含缓存、localStorage 等），不是本应用精确大小。文案必须说清楚这一点，
 *   否则用户会拿它对账。
 */

import { db } from '../db';

export interface LocalDataStats {
  /** 未删除的会话数。 */
  sessions: number;
  /** 本机会话里的消息总数（不含墓碑会话里的——它们已经被删了）。 */
  messages: number;
  /** 浏览器估算的 origin 存储占用（字节）；不被支持或拒绝时为 null。 */
  usageBytes: number | null;
}

/** 浏览器可能不提供 storage.estimate，也可能在隐私模式下拒绝：两种情况都退回 null。 */
async function estimateUsageBytes(): Promise<number | null> {
  try {
    const storage = typeof navigator === 'undefined' ? undefined : navigator.storage;
    if (!storage || typeof storage.estimate !== 'function') return null;
    const estimate = await storage.estimate();
    return typeof estimate?.usage === 'number' ? estimate.usage : null;
  } catch (err) {
    console.warn('读取浏览器存储占用失败:', err);
    return null;
  }
}

/** 读一次本机数据统计（不缓存：设置面板每次打开都读最新的）。 */
export async function readLocalDataStats(): Promise<LocalDataStats> {
  // 没有事务包住：两个 count 之间用户可能删了东西，但这是一份展示用的快照，
  // 数字之间的微小偏差不值得为它开一个长事务。
  const [sessions, messages] = await Promise.all([
    db.sessions.filter((session) => !session.deleted).count(),
    db.messages.count(),
  ]);

  return { sessions, messages, usageBytes: await estimateUsageBytes() };
}

/**
 * 人类可读的字节数。
 *
 * 用 1024 进制并在单位间保留一位小数：用户在系统设置里看到的也是这一套。`null` 明确
 * 显示「未知」而不是 0——0 B 会被理解成「没占空间」。
 */
export function formatBytes(bytes: number | null): string {
  if (bytes === null || !Number.isFinite(bytes) || bytes < 0) return '未知';
  if (bytes < 1024) return `${Math.round(bytes)} B`;

  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(1)} ${units[unitIndex]}`;
}
