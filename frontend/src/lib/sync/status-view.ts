/**
 * 同步状态的展示模型（纯函数）。
 *
 * 引擎只给事实（待推送几行、被拒几行、上次成功时间、错误文本），「该怎么说」归这里。
 * 把措辞与优先级从组件里拆出来的理由：状态优先级很容易写错——例如既有错误提示又有
 * 待推送数量时，得先说错误，否则用户看到的是「两条待同步」而不是「同步失败了」。
 *
 * 与引擎分开的另一层原因：这里能在 node 环境直接测，不需要 IndexedDB / 网络。
 */

import type { SyncStatus } from './engine';

export type SyncTone = 'idle' | 'ok' | 'busy' | 'warning' | 'error';

export interface SyncStatusView {
  tone: SyncTone;
  /** 面板里的一句话结论。 */
  headline: string;
  /** 结论下面的补充说明（可能为空串）。 */
  detail: string;
  /** 是否应该出现「重试」入口。 */
  canRetry: boolean;
  /** 角标数字：还只在本机、没落到服务器上的行数。 */
  badgeCount: number;
}

/**
 * 服务端拒收原因的文案。
 *
 * 只列**会变成 error 状态、永久停在本机**的原因（见 engine 的 `settleRow`）：
 * `stale` / `not_owned` / `conversation_deleted` 会被当作已同步，不会出现在这里。
 */
const REJECTION_REASON_TEXT: Record<string, string> = {
  too_large: '单条内容超过服务器上限',
  quota_exceeded: '服务器存储配额已满',
  conversation_missing: '所属会话在服务器上已不存在',
};

/** 未知原因也给一句人话，不要把机器码直接甩给用户。 */
export function describeRejectionReason(reason: string | null): string {
  if (!reason) return '服务器拒绝了这些内容';
  return REJECTION_REASON_TEXT[reason] ?? `服务器拒绝了这些内容（${reason}）`;
}

/**
 * 相对时间。
 *
 * 未来时间（本机时钟比服务器快）按「刚刚」处理：显示「-3 分钟前」只会让人以为坏了。
 * 超过 30 天不再往「N 天前」堆，直接给日期——那个尺度上相对时间已经帮不上判断。
 */
export function formatRelativeTime(at: number | null, now: number): string {
  if (at === null) return '从未';
  const diff = Math.max(0, now - at);
  if (diff < 5_000) return '刚刚';
  if (diff < 60_000) return `${Math.floor(diff / 1_000)} 秒前`;
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  if (diff < 30 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`;
  return new Date(at).toLocaleDateString('zh-CN');
}

/** 面板里「上次同步」的 tooltip 用精确时间，避免相对时间含糊。 */
export function formatAbsoluteTime(at: number): string {
  return new Date(at).toLocaleString('zh-CN', { hour12: false });
}

/**
 * 状态优先级：错误 > 被拒 > 待推送 > 首次同步 > 已同步。
 *
 * 错过的顺序会直接误导：既有 `lastError` 又有待推送行时先说「N 条待同步」，
 * 用户会以为只是慢了一点；而被服务端明确拒收的行永远不会自己好，
 * 必须比「还在排队」更靠前。
 */
export function describeSyncStatus(status: SyncStatus): SyncStatusView {
  const unsynced = status.pending + status.rejected;

  if (!status.running) {
    return {
      tone: 'idle',
      headline: '同步未启用',
      detail: '登录后会在后台自动同步对话。',
      canRetry: false,
      badgeCount: 0,
    };
  }

  if (status.lastError) {
    return {
      tone: 'error',
      headline: '同步失败',
      detail: status.lastError,
      canRetry: true,
      badgeCount: unsynced,
    };
  }

  if (status.rejected > 0) {
    return {
      tone: 'warning',
      headline: `${status.rejected} 条内容无法同步`,
      detail: `${describeRejectionReason(status.rejectedReason)}；这些内容只保存在本机浏览器中。`,
      canRetry: true,
      badgeCount: unsynced,
    };
  }

  if (status.pending > 0) {
    return {
      tone: 'busy',
      headline: status.syncing ? '正在上传变更…' : `${status.pending} 条变更等待上传`,
      detail: status.syncing
        ? '上传完成后会自动拉取其他设备的最新内容。'
        : '网络或服务器恢复后会自动重试，不需要手动处理。',
      canRetry: false,
      badgeCount: unsynced,
    };
  }

  if (status.lastSyncedAt === null) {
    return {
      tone: 'busy',
      headline: '正在建立同步…',
      detail: '首次同步完成后，这里会显示最近一次同步时间。',
      canRetry: false,
      badgeCount: 0,
    };
  }

  return {
    tone: 'ok',
    headline: '已同步',
    detail: '所有对话都已保存到服务器，换设备登录同一账号即可看到。',
    canRetry: false,
    badgeCount: 0,
  };
}
