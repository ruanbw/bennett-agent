/**
 * 「我的电脑」指示器的展示模型（纯函数）。
 *
 * 与 `lib/sync/status-view.ts` 同一个套路：客户端只给事实（租约状态、产物列表、错误），
 * 「该怎么说」全部归这里。这样做有两个具体好处：
 *
 * 1. 文案与优先级可以脱离 DOM 在 node 环境里钉住——状态优先级很容易写错（例如探测失败
 *    时若还显示上一轮的「已就绪」，用户会以为机器还好着）；
 * 2. 「未知状态」（后端新增一档）与「没有机器」必须被区分开，而这种区分只能靠断言锁住。
 *
 * ## 叫「我的电脑」，不叫「本会话的电脑」
 *
 * 机器是**每用户一台、跨会话共用**的（后端 `lease.py`）。把 `session_id` 渲染成「这台
 * 机器属于这个会话」会直接误导用户：他会以为开个新会话就能得到一台新机器，或者关掉
 * 会话机器就没了。`session_id` 在这里的唯一含义是「当前打开的是哪个会话，用户此刻想
 * 不想看这块东西」。
 */

import type { SandboxArtifact, SandboxStatus } from '../../lib/sandbox';
import { describeSandboxState } from '../../lib/sandbox';

export type SandboxTone = 'muted' | 'ok' | 'busy' | 'warning' | 'error';

/** 指示器上固定的名称。 */
export const SANDBOX_LABEL = '我的电脑';

export interface SandboxStatusView {
  tone: SandboxTone;
  /** 指示器上的固定名称（「我的电脑」）。 */
  label: string;
  /** 状态短语：「已就绪」「正在启动」「还没有启动」…… */
  stateLabel: string;
  /** 面板标题：`我的电脑 · 已就绪`。 */
  title: string;
  /** 悬浮/无障碍名称：比可见文本多说一点，说明「为什么是这个状态」。 */
  description: string;
  /** 状态点是否该做呼吸动画（正在启动/回收这类过渡态）。 */
  pulsing: boolean;
}

/** 组装 view；标题的拼接规则只写一次，避免各分支各拼一遍拼出不一致的标题。 */
function makeView(
  tone: SandboxTone,
  stateLabel: string,
  description: string,
  pulsing = false,
): SandboxStatusView {
  return {
    tone,
    label: SANDBOX_LABEL,
    stateLabel,
    title: `${SANDBOX_LABEL} · ${stateLabel}`,
    description,
    pulsing,
  };
}

/**
 * 探测失败**不能**沿用上一次的成功状态。
 *
 * 传入 `failed: true` 时无论 `status` 是什么都给出「暂时查不到」：机器可能已经被回收，
 * 而界面还挂着绿色的「已就绪」——这比不显示更糟，用户会去等一个不存在的环境。
 */
export function describeSandboxStatus(
  status: SandboxStatus | null | undefined,
  failed = false,
): SandboxStatusView {
  if (failed) {
    return makeView('warning', '暂时查不到', '暂时查不到我的电脑的状态，稍后会自动重试');
  }

  if (!status) {
    // 404 = 这个会话还没有过环境。不能说成「已回收」：那暗示曾经有、现在没了，
    // 而真实情况可能只是用户还没用过沙箱工具（机器会在第一次用到时自动创建）。
    return makeView('muted', '还没有启动', '还没有启动。用到需要执行的环境时会自动创建');
  }

  switch (status.state) {
    case 'ready':
      return makeView('ok', '已就绪', '我的电脑已就绪，可以执行任务');
    case 'provisioning':
      return makeView('busy', '正在启动', '我的电脑正在启动，稍等片刻', true);
    case 'destroying':
      return makeView('busy', '正在回收', '我的电脑正在回收', true);
    case 'paused':
      return makeView('warning', '已暂停', '我的电脑已暂停，下次用到时会自动恢复');
    case 'destroyed':
      return makeView('muted', '已回收', '我的电脑已回收，闲置超时会自动回收，需要时再创建');
    case 'destroy_failed':
      return makeView('warning', '回收失败', '我的电脑回收失败，暂时可能仍占用资源');
    case 'failed':
      return makeView('error', '启动失败', '我的电脑启动失败，可用工具暂时不可用');
    default: {
      // 后端新增了一档：如实带出原始值（`describeSandboxState` 会给「状态未知（x）」），
      // 不要显示成「从未创建」——那会让用户以为数据没了，也会让排障的人查错方向。
      const raw = describeSandboxState(status.state);
      return makeView('muted', raw, `我的电脑${raw}`);
    }
  }
}

/** 产物列表为空时的一句话。 */
export function describeArtifactCount(artifacts: readonly SandboxArtifact[]): string {
  return artifacts.length > 0 ? `${artifacts.length} 个文件` : '暂无文件';
}

/**
 * 预签名链接的剩余寿命。
 *
 * 必须显示：链接是**有时效**的，用户点开一个几分钟前生成的链接会拿到 403，而界面上
 * 看起来一切正常。说明「还有多久」比事后解释 403 有用得多。
 *
 * @param expiresAt 服务端给的时刻（ISO 字符串）
 * @param now 当前时刻（epoch ms）——传进来而不是内部取 `Date.now()`，测试才能钉住
 */
export function describeArtifactExpiry(expiresAt: string, now: number): string {
  if (!expiresAt) return '';
  const at = Date.parse(expiresAt);
  if (!Number.isFinite(at)) return '';

  const remainingMs = at - now;
  if (remainingMs <= 0) return '下载链接已失效，请点「刷新」重新获取';

  const minutes = Math.floor(remainingMs / 60_000);
  if (minutes < 1) return '下载链接 1 分钟内失效';
  // 一小时以上没必要精确到分钟：用户只需要知道「现在点没问题」
  if (minutes >= 60) {
    const hours = Math.floor(minutes / 60);
    return `下载链接 ${hours} 小时后失效`;
  }
  return `下载链接 ${minutes} 分钟后失效`;
}

/**
 * 产物列表是否整体已过期。
 *
 * 面板据此把底部说明从「多久后失效」换成「点刷新重新获取」，而不是让用户去点一堆
 * 注定 403 的链接。
 */
export function artifactsExpired(artifacts: readonly SandboxArtifact[], now: number): boolean {
  // 空列表谈不上过期（没有可点的东西）
  if (artifacts.length === 0) return false;
  // 只要还有一个链接可用，就还能下载东西，不该把整块说成失效
  return artifacts.every((artifact) => artifactExpired(artifact.expires_at, now));
}

/** 单个产物是否已过期（行内要把「下载」灰掉）。 */
export function artifactExpired(expiresAt: string, now: number): boolean {
  const at = Date.parse(expiresAt);
  return Number.isFinite(at) && at <= now;
}
