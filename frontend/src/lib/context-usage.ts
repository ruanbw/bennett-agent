/**
 * 上下文用量的口径与展示口径。
 *
 * 只做「把一个真值翻译成可读数字」，不做估算：`contextTokens` 来自上游回报的
 * `input_tokens`（见 `app/services/usage.py`），它包含系统提示词与整份历史，
 * 也就是**当前上下文长度**的唯一准确值。
 *
 * 为什么不承认「估算」：同一份历史在不同分词器下长度不同，估出来的数会与真正
 * 触发上下文锁的那个数对不上——用户看到 60% 却被拒绝，比看不到数字更糟。
 */

import { readUsageTiming } from './usage-timing';
import type { UsageState } from '@/types';

/** 占比小于这个值时不说「0.0%」：那看起来像「没用量」，其实是「用量很小」。 */
const TINY_PERCENT = 0.1;

/**
 * 上下文占用到这些比例时的提醒档位。
 *
 * 放在这里而不是组件里：进度条变红与「已接近上限」文案必须同时发生。此前组件里
 * 90 写了两遍（`nearFull` 与 `UsageBar` 的 tone）、70 只写了一遍——改一个忘一个就会
 * 出现「进度条已经红了但没有提示」，或者反过来。
 */
export const CONTEXT_WARN_PERCENT = 70;
export const CONTEXT_CRITICAL_PERCENT = 90;

const positive = (value: number | null | undefined): number | null =>
  typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null;

/** 千位分隔与 k/M 缩写：1,066 / 12k / 1.0M。 */
export function formatTokens(value: number): string {
  if (!Number.isFinite(value) || value < 0) return '—';
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 10_000) return `${Math.round(value / 1000)}k`;
  return Math.round(value).toLocaleString('en-US');
}

/** 占比文案：夹在 0–100，小于 0.1% 的写成 `<0.1%` 而不是 0.0%。 */
export function formatPercent(percent: number): string {
  if (!Number.isFinite(percent)) return '—';
  const clamped = Math.min(Math.max(percent, 0), 100);
  if (clamped > 0 && clamped < TINY_PERCENT) return '<0.1%';
  return `${clamped.toFixed(1)}%`;
}

/**
 * 剩余空间换算成「大约还能写多少字」。
 *
 * token 是模型账本上的单位，用户对它的直觉很差：看到「还剩 18k tokens」既不知道这是
 * 多还是少，也不知道够写几段话。这里按**中文常见分词口径**粗估（≈1 token/字），
 * 只当作量级参照——弹层里同时写明「粗估」，不假装它是精确换算：
 * 英文约 4 字符/token，同一串剩余量能写的英文单词数会少得多。
 *
 * 未知（`null`）或已经没有余量时返回空串：调用方据此不渲染那一行，而不是写「约 0 字」。
 */
export function estimateRemainingChars(remainingTokens: number | null): string {
  if (remainingTokens === null || !Number.isFinite(remainingTokens) || remainingTokens <= 0) {
    return '';
  }
  return `约 ${formatTokens(remainingTokens)} 字`;
}

/**
 * 收起态的 token 格式：k 级保留一位小数。
 *
 * `formatTokens` 对 10k 以上直接取整，17,500 与 18,000 都会变成「18k」——进度条已经
 * 97% 了，收起态却显示「18k / 18k」，看起来像没用量。这里只在收起态补一位小数，
 * 模型菜单里的窗口徽标仍用整数 k，不受影响。
 */
function formatCompactTokens(value: number): string {
  if (value >= 10_000 && value < 100_000) {
    const thousands = Math.round((value / 1000) * 10) / 10;
    return `${thousands}k`;
  }
  return formatTokens(value);
}

/**
 * `metadata.custom.usage` 是运行时透传的任意形状：按鸭子类型取，缺字段/写坏就当没有。
 *
 * 校验放在组件外而不是 `useAuiState` 的 selector 里：selector 必须返回稳定引用，
 * 在里面构造新对象会让快照每次都变（同一轮会无限重渲染）。
 */
export function readUsage(value: unknown): UsageState | null {
  if (!value || typeof value !== 'object') return null;
  const {
    contextTokens,
    contextWindow,
    contextLimitTokens,
    outputTokens,
    cacheReadTokens,
  } = value as Partial<UsageState>;
  if (typeof contextTokens !== 'number' || !Number.isFinite(contextTokens)) return null;
  return {
    contextTokens,
    contextWindow: typeof contextWindow === 'number' ? contextWindow : null,
    contextLimitTokens: typeof contextLimitTokens === 'number' ? contextLimitTokens : null,
    outputTokens: typeof outputTokens === 'number' ? outputTokens : 0,
    cacheReadTokens: typeof cacheReadTokens === 'number' ? cacheReadTokens : 0,
    // 计时明细单独走 `readUsageTiming`（容错：缺 steps/turnMs 就整块算没有）。
    // 这里**不能**自己逐字段校验：那条口径已经写在 usage-timing.ts 里，复制一遍
    // 就会出现两套「什么算有效计时」的判定。上下文占比与计时是两件事，
    // `describeContextUsage` 刻意不碰它。
    timing: readUsageTiming((value as Partial<UsageState>).timing),
  };
}

export interface ContextUsageOptions {
  /** 当前选中的模型 id：可能是刚切换、还没发过消息的那个。 */
  currentModel?: string | null;
  /** 产生这份用量的模型 id（消息上的 `metadata.custom.model`）；老记录缺失时为 null。 */
  usageModel?: string | null;
  /** 当前选中模型的窗口（`/api/models` 的 `context_window`）；网关没声明时为 null。 */
  modelContextWindow?: number | null;
}

/** 窗口是从哪来的：当前模型配置 / 上游回报 / 不知道（`null`）。 */
export type ContextWindowSource = 'model' | 'reported' | null;

/**
 * 进度条分母的来源。
 *
 * - `budget`：后端判定超限时用的真实 prompt 上限（窗口扣掉回答预留与安全余量）。
 *   这是唯一与「下一轮会不会被拒」对齐的口径；
 * - `window`：拿不到预算时的退回口径（换模型后预算属于旧模型，不能继续用）；
 * - `null`：连窗口都不知道，只展示绝对值。
 */
export type ContextLimitSource = 'budget' | 'window' | null;

export interface ContextUsageView {
  /** 上一轮请求的真实 prompt token 数 = 当前上下文长度。 */
  contextTokens: number;
  /** 模型的原始窗口；不知道时为 null。 */
  contextWindow: number | null;
  windowSource: ContextWindowSource;
  /**
   * 进度条与「剩余」的分母：优先用后端预算（`contextLimitTokens`），没有预算时退回窗口。
   * 不知道时为 null（此时不编造占比）。
   */
  limitTokens: number | null;
  limitSource: ContextLimitSource;
  /** 已用占「可用上限」的比例 0–100；未知时为 null。 */
  usedPercent: number | null;
  /** 还能再装多少 token；未知时为 null。不会为负。 */
  remainingTokens: number | null;
  remainingPercent: number | null;
  /** 命中上游提示缓存的 token（是 `contextTokens` 的子集，不是额外开销）。 */
  cacheReadTokens: number;
  /** 缓存命中占已用上下文的比例 0–100；没有上下文时为 null。 */
  cachePercent: number | null;
  /** 本轮生成的总输出 token（只展示，不参与上下文占比）。 */
  outputTokens: number;
  /** 收起状态的那一小段字：已用 / 可用上限（或窗口）。 */
  compactLabel: string;
  /** 收起状态的 aria-label：带占比，窗口与预算口径都写清楚。 */
  summary: string;
}

/**
 * 把一轮真实用量翻译成展示用的数字。
 *
 * 两个分母的坑要分开说：
 *
 * 1. **预算 vs 窗口**：后端判定超限用的是「窗口 - 回答预留」再乘安全系数，而
 *    `contextTokens` 是含系统提示词的**真实** prompt。拿原始窗口当分母会把「已经
 *    用掉 90% 预算」显示成「才用了 56%」——用户看着还很宽敞，下一轮却被拒。
 *    所以优先用后端随用量回来的 `contextLimitTokens`。
 * 2. **切模型**：用量与预算都属于**上一轮回答的那个模型**。切到新模型后不能拿旧
 *    预算继续算（旧模型 1M、新模型 32k，显示 5% 而下一轮直接被锁），此时退回当前
 *    模型的窗口；新窗口也不知道就认「不知道」，只报绝对值。
 */
export function describeContextUsage(
  usage: UsageState | null | undefined,
  options: ContextUsageOptions = {},
): ContextUsageView | null {
  if (!usage) return null;
  const contextTokens = Math.max(Math.round(usage.contextTokens), 0);
  if (!Number.isFinite(contextTokens)) return null;

  const modelWindow = positive(options.modelContextWindow);
  const reportedWindow = positive(usage.contextWindow);
  const sameModel =
    !options.currentModel || !options.usageModel || options.currentModel === options.usageModel;
  const contextWindow = modelWindow ?? (sameModel ? reportedWindow : null);
  const windowSource: ContextWindowSource =
    contextWindow === null ? null : modelWindow !== null ? 'model' : 'reported';

  // 预算只对产生它的那个模型有效；切换模型后必须丢掉，不能把旧模型的余量当成新模型的
  const budgetLimit = sameModel ? positive(usage.contextLimitTokens) : null;
  const limitTokens = budgetLimit ?? contextWindow;
  const limitSource: ContextLimitSource =
    budgetLimit !== null ? 'budget' : contextWindow !== null ? 'window' : null;

  const usedPercent =
    limitTokens === null ? null : Math.min((contextTokens / limitTokens) * 100, 100);
  const remainingTokens =
    limitTokens === null ? null : Math.max(limitTokens - contextTokens, 0);
  const remainingPercent = usedPercent === null ? null : Math.max(100 - usedPercent, 0);

  const cacheReadTokens = Math.max(Math.round(usage.cacheReadTokens) || 0, 0);
  const cachePercent =
    contextTokens > 0 ? Math.min((Math.min(cacheReadTokens, contextTokens) / contextTokens) * 100, 100) : null;
  const outputTokens = Math.max(Math.round(usage.outputTokens) || 0, 0);

  // 收起状态报「已用 / 分母」这种绝对值，而不是占比：个位数百分比在大窗口上也能
  // 一直是 0.1%，看久了没有信息量；占比留给弹层和 aria-label。
  const compactLabel =
    limitTokens === null
      ? formatCompactTokens(contextTokens)
      : `${formatCompactTokens(contextTokens)} / ${formatCompactTokens(limitTokens)}`;
  const windowNote = contextWindow === null ? '' : `；模型窗口 ${formatTokens(contextWindow)}`;
  const summary =
    limitTokens === null
      ? `上下文 ${formatTokens(contextTokens)} tokens（窗口未知）`
      : limitSource === 'budget'
        ? `上下文 ${formatTokens(contextTokens)} / 可用上限 ${formatTokens(limitTokens)}（${formatPercent(usedPercent ?? 0)}${windowNote}）`
        : `上下文 ${formatTokens(contextTokens)} / ${formatTokens(limitTokens)}（${formatPercent(usedPercent ?? 0)}）`;

  return {
    contextTokens,
    contextWindow,
    windowSource,
    limitTokens,
    limitSource,
    usedPercent,
    remainingTokens,
    remainingPercent,
    cacheReadTokens,
    cachePercent,
    outputTokens,
    compactLabel,
    summary,
  };
}
