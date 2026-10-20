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

import type { UsageState } from '@/types';

/** 占比小于这个值时不说「0.0%」：那看起来像「没用量」，其实是「用量很小」。 */
const TINY_PERCENT = 0.1;

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
 * `metadata.custom.usage` 是运行时透传的任意形状：按鸭子类型取，缺字段/写坏就当没有。
 *
 * 校验放在组件外而不是 `useAuiState` 的 selector 里：selector 必须返回稳定引用，
 * 在里面构造新对象会让快照每次都变（同一轮会无限重渲染）。
 */
export function readUsage(value: unknown): UsageState | null {
  if (!value || typeof value !== 'object') return null;
  const { contextTokens, contextWindow, outputTokens, cacheReadTokens } = value as Partial<UsageState>;
  if (typeof contextTokens !== 'number' || !Number.isFinite(contextTokens)) return null;
  return {
    contextTokens,
    contextWindow: typeof contextWindow === 'number' ? contextWindow : null,
    outputTokens: typeof outputTokens === 'number' ? outputTokens : 0,
    cacheReadTokens: typeof cacheReadTokens === 'number' ? cacheReadTokens : 0,
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

export interface ContextUsageView {
  /** 上一轮请求的真实 prompt token 数 = 当前上下文长度。 */
  contextTokens: number;
  /** 用于算占比的窗口；不知道时为 null（此时只报绝对值，不编造占比）。 */
  contextWindow: number | null;
  windowSource: ContextWindowSource;
  /** 已用占比 0–100；窗口未知时为 null。 */
  usedPercent: number | null;
  /** 窗口还装得下多少 token；窗口未知时为 null。不会为负。 */
  remainingTokens: number | null;
  remainingPercent: number | null;
  /** 命中上游提示缓存的 token（是 `contextTokens` 的子集，不是额外开销）。 */
  cacheReadTokens: number;
  /** 缓存命中占已用上下文的比例 0–100；没有上下文时为 null。 */
  cachePercent: number | null;
  /** 本轮生成的总输出 token（只展示，不参与上下文占比）。 */
  outputTokens: number;
  /** 收起状态的那一小段字：已用 / 窗口（窗口未知时只有已用）。 */
  compactLabel: string;
  /** 收起状态的 title，鼠标悬停第一层就能看到完整数字。 */
  summary: string;
}

/**
 * 把一轮真实用量翻译成展示用的数字。
 *
 * 窗口的取法有个坑：用量是**上一轮**的，而用户可能已经切了模型。这时上游回报的
 * 窗口属于旧模型，拿它算占比会给出错误的安全感（旧模型 1M、新模型 32k，显示 5%
 * 而下一轮直接被锁）。所以：
 *
 * 1. 当前模型的窗口已知 → 用它（后端预检也是按它判定的）；
 * 2. 模型没变（或模型 id 不知道）→ 退回上游回报的窗口；
 * 3. 模型变了且新窗口未知 → 认「不知道」，只报绝对值。
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

  const usedPercent =
    contextWindow === null ? null : Math.min((contextTokens / contextWindow) * 100, 100);
  const remainingTokens =
    contextWindow === null ? null : Math.max(contextWindow - contextTokens, 0);
  const remainingPercent = usedPercent === null ? null : Math.max(100 - usedPercent, 0);

  const cacheReadTokens = Math.max(Math.round(usage.cacheReadTokens) || 0, 0);
  const cachePercent =
    contextTokens > 0 ? Math.min((Math.min(cacheReadTokens, contextTokens) / contextTokens) * 100, 100) : null;
  const outputTokens = Math.max(Math.round(usage.outputTokens) || 0, 0);

  // 收起状态报「已用 / 窗口」这种绝对值，而不是占比：个位数百分比在小窗口上也能
  // 一直是 0.1%，看久了没有信息量；占比留给弹层和 title。
  const compactLabel =
    contextWindow === null
      ? formatTokens(contextTokens)
      : `${formatTokens(contextTokens)} / ${formatTokens(contextWindow)}`;
  const summary =
    contextWindow === null
      ? `上下文 ${formatTokens(contextTokens)} tokens（窗口未知）`
      : `上下文 ${formatTokens(contextTokens)} / ${formatTokens(contextWindow)}（${formatPercent(usedPercent ?? 0)}）`;

  return {
    contextTokens,
    contextWindow,
    windowSource,
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
