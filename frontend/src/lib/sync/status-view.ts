/**
 * 同步状态的展示模型（纯函数）。
 *
 * 引擎只给事实（待推送几行、被拒几行、上次成功时间、错误文本），「该怎么说」归这里。
 * 把措辞与优先级从组件里拆出来的理由：状态优先级很容易写错——例如既有错误提示又有
 * 待推送数量时，得先说错误，否则用户看到的是「两条待同步」而不是「同步失败了」。
 *
 * 与引擎分开的另一层原因：这里能在 node 环境直接测，不需要 IndexedDB / 网络。
 */

import { describeConnectionFailure } from '../connection-failure';
import { SyncHttpError } from './api';
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
 * 被拒内容还能不能自救。
 *
 * 「单条内容超过服务器上限」重试多少次都一样——数据本身放不进去，用户需要去删掉或导出
 * 那条内容；而配额满、会话在服务器上不存在，重新上传有可能成功。面板据此决定给不给
 * 「重新上传」按钮：给一个注定失败的重试按钮，比不给更让人困惑。
 */
export function isRejectionRetryable(reason: string | null): boolean {
  return reason !== 'too_large';
}

/** 面板里「待上传」一行的提示：正在上传时不要再说「无」。 */
export function describePendingCount(pending: number, syncing: boolean): string {
  if (pending > 0) return `${pending} 条`;
  return syncing ? '正在检查…' : '无';
}

/**
 * 本机修改被服务器版本覆盖时的说明。
 *
 * `stale` 的处理是「标 synced，下一轮 pull 用服务端版本盖掉本地」——本机那次修改就
 * 丢了。这条提示必须在面板里出现：用户改了标题、看到「已同步」，换设备才发现还是旧标题，
 * 那是最难排查的一类「同步没坏但我的改动没了」。
 */
export function describeSuperseded(count: number): string {
  if (count <= 0) return '';
  return `${count} 处本机修改被其他设备的更新版本覆盖（以服务器上的为准）。`;
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
 * 把同步失败翻译成用户能读的一句话。
 *
 * 引擎此前直接把 `err.message` 塞进 `status.lastError`：长轮询超时时面板上写的是
 * 「同步失败 signal timed out」——英文、且不说明「这是超时、可以重试」。HTTP 失败同理，
 * 会漏出「HTTP 500」这类原始信息。这里复用对话侧的 `describeConnectionFailure`：
 * 超时 → 「请求超时，请重试」、断网 → 「网络连接中断…」、5xx → 「服务器暂时不可用…」。
 *
 * `SyncHttpError` 自带服务端给的 detail（已是中文、且带原因），优先用它。
 */
export function describeSyncFailure(err: unknown): string {
  if (err instanceof SyncHttpError) return err.message;
  return describeConnectionFailure(err).message;
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
    // 本机修改被覆盖时不能再只说「所有对话都已保存到服务器」——那句话会让人以为
    // 自己刚改的内容也上去了，而实际上被服务端版本取代了。
    detail:
      (status.superseded ?? 0) > 0
        ? '服务器上的内容是最新的；本机有修改被其他设备的更新版本覆盖，详见下方说明。'
        : '所有对话都已保存到服务器，换设备登录同一账号即可看到。',
    canRetry: false,
    badgeCount: 0,
  };
}
