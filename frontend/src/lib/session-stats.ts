/**
 * 会话级统计：把整段对话里的计时与用量聚合成输入框下方那排药丸要用的几个数。
 *
 * ## 数据从哪来
 *
 * - 轮次 / 步数 / 用时 / 速度：每条助手消息 `metadata.custom.usage.timing`
 *   （`UsageTiming`，服务端实测；见 `app/schemas/events.py`）。
 * - token 总量 / 缓存命中：每条助手消息的 `usage`（`contextTokens` / `cacheReadTokens`
 *   / `outputTokens`）。
 *
 * ## 拿不到就不显示，绝不补 0
 *
 * 后端补计时字段是逐步落地的：老记录、旧服务端、上游不回 usage 的轮次都可能没有
 * `timing`。这时**少一个药丸**，而不是显示「0 tok/s」——后者会被读成「模型卡住了」，
 * 「首字 0ms」会被读成「秒回」，两者都是错误结论。本文件里所有拿不到的量都返回
 * `null`，由组件决定不渲染。
 *
 * ## 速度的分母必须是解码阶段
 *
 * 一轮里只要有工具调用就有多次模型请求，中间还夹着工具执行与排队。`decodeTokens /
 * decodeMs` 才是「模型吐字多快」；拿整轮墙钟（`turnMs`）当分母会把工具耗时算进生成
 * 速度，得到一个「模型很慢」的错误结论。所以这里只聚合 `decodeMs` / `decodeTokens`，
 * 除法交给 `usage-timing.decodeTokensPerSecond`。
 *
 * ## 缓存命中率的分母是「三个 prompt 侧桶之和」
 *
 * 上游把一条 prompt 拆成三个互不重叠的计费桶：未命中缓存的输入、缓存读取、缓存写入。
 * 缓存命中率 = 缓存读取 / 三者之和，这是**计费口径**。
 *
 * 与 `lib/context-usage.ts` 的 `cachePercent` 刻意不同：那个算的是「当前上下文里有多大
 * 比例来自缓存」（分子分母都是**同一轮**的 prompt，回答「这轮上下文复用得好不好」）。
 * 这里的 session 口径要把**每一轮**的 prompt 都算进分母——历史每轮都会重发一遍，虽然
 * 内容重叠，但每一轮都真的被计费了。两个数常常很接近、但不是同一个概念，不能互替。
 *
 * 本项目后端目前只回报 `cacheReadTokens`（没有单列 uncached / cacheWrite），所以
 * uncached = `contextTokens - cacheReadTokens` 是**推导值**，`cacheWriteTokens` 恒为
 * `null`（未知），弹层里据此隐藏「缓存写入」那一行，而不是显示 0。
 *
 * 本文件是纯 TS（不依赖 React），可以直接在 node 环境的 vitest 里测。
 */

import { readUsage } from './context-usage';
import { decodeTokensPerSecond, readUsageTiming } from './usage-timing';
import type { UsageTiming } from '../types';

/**
 * 本文件接受的「消息」形状。
 *
 * 只用两个字段，并且刻意写得很窄：assistant-ui 的 `ThreadMessage` 与渲染层的
 * `ThreadMessageLike` 都结构兼容，调用方不必把消息先转成别的形状（转一次就多一个
 * 新数组，而 selector 返回新引用会让组件无限重渲染）。
 */
export interface StatsMessageLike {
  readonly role?: string;
  readonly metadata?:
    | {
        readonly custom?: Record<string, unknown> | undefined;
      }
    | undefined;
}

/** 整段会话的计时聚合。拿不到的量是 `null`，不是 0。 */
export interface SessionStats {
  /**
   * 有计时数据的轮次数。
   *
   * 只数**有 `timing` 的轮**：轮次与步数必须覆盖同一批轮次。混着数会出现「5 轮 2 步」
   * 这种自相矛盾的读数（3 轮是没计时的老记录），比只报部分轮次更误导。
   */
  turns: number;
  /** 累计模型请求次数（一轮里可能有多次，例如中间调了工具）。 */
  steps: number;
  /** 模型请求占用的累计时间（不是整轮墙钟：排队与工具执行不算）。 */
  llmMs: number;
  /** 工具执行占用的累计时间。 */
  toolMs: number;
  /** 步均首 token 延迟（各轮按步数加权）；没有样本时为 null。 */
  ttftMs: number | null;
  /** 参与 TTFT 加权的步数；0 表示没有首字样本。 */
  ttftSteps: number;
  /** 解码阶段的累计耗时。 */
  decodeMs: number;
  /** 解码阶段产出的累计 token 数。 */
  decodeTokens: number;
  /** 至少有一条消息带计时数据；false 时 activity 药丸整体不渲染。 */
  hasTiming: boolean;
}

/** 空聚合：没有消息、或一条带计时的消息都没有。 */
export const EMPTY_SESSION_STATS: SessionStats = {
  turns: 0,
  steps: 0,
  llmMs: 0,
  toolMs: 0,
  ttftMs: null,
  ttftSteps: 0,
  decodeMs: 0,
  decodeTokens: 0,
  hasTiming: false,
};

/** 整段会话的 token 账单聚合。 */
export interface SessionTokenTotals {
  /** 未命中缓存的 prompt token（**推导值**：prompt - 命中部分）。 */
  uncachedInputTokens: number;
  /** 命中提示缓存的 prompt token。 */
  cacheReadTokens: number;
  /**
   * 缓存写入 token；本项目上游不回报，恒为 `null`（未知）。
   *
   * 与「0」不同：0 表示「确实没写缓存」，`null` 表示「不知道」——弹层据此隐藏该行。
   */
  cacheWriteTokens: number | null;
  /** 累计输出 token。 */
  outputTokens: number;
  /** 三个 prompt 侧桶之和 = 计费输入。 */
  billedInputTokens: number;
  /** prompt 侧 + 输出，就是药丸上那个总量。 */
  totalTokens: number;
  /** 有 usage 的轮次数。 */
  turns: number;
}

/** 空账单。 */
export const EMPTY_SESSION_TOKENS: SessionTokenTotals = {
  uncachedInputTokens: 0,
  cacheReadTokens: 0,
  cacheWriteTokens: null,
  outputTokens: 0,
  billedInputTokens: 0,
  totalTokens: 0,
  turns: 0,
};

/** 读一条消息上的 `metadata.custom`；缺失/写坏当作没有。 */
function readCustom(message: StatsMessageLike): Record<string, unknown> | null {
  const custom = message.metadata?.custom;
  return custom && typeof custom === 'object' ? custom : null;
}

/** 只收集助手消息的 usage：用户消息上没有用量。 */
function readAssistantUsage(message: StatsMessageLike): ReturnType<typeof readUsage> {
  if (message.role !== 'assistant') return null;
  return readUsage(readCustom(message)?.usage);
}

const nonNegative = (value: number): number => (Number.isFinite(value) && value > 0 ? value : 0);

/**
 * 聚合整段会话的计时。
 *
 * 每轮 `timing` 里：
 * - `steps` / `llmMs` / `toolMs` / `decodeMs` / `decodeTokens` 直接累加；
 * - `ttftMs` 是**该轮的步均首字延迟**，跨轮求平均必须按步数加权，否则一个 1 步的轮
 *   与一个 10 步的轮会等权，把平均值拉偏。
 */
export function aggregateSessionStats(
  messages: readonly StatsMessageLike[],
): SessionStats {
  let turns = 0;
  let steps = 0;
  let llmMs = 0;
  let toolMs = 0;
  let ttftWeightedSum = 0;
  let ttftSteps = 0;
  let decodeMs = 0;
  let decodeTokens = 0;

  for (const message of messages) {
    const usage = readAssistantUsage(message);
    if (!usage) continue;
    const timing = readUsageTiming(usage.timing);
    if (!timing) continue;

    turns += 1;
    steps += timing.steps;
    llmMs += timing.llmMs;
    toolMs += timing.toolMs;
    decodeMs += nonNegative(timing.decodeMs ?? 0);
    decodeTokens += nonNegative(timing.decodeTokens ?? 0);
    if (timing.ttftMs !== null && timing.steps > 0) {
      ttftWeightedSum += timing.ttftMs * timing.steps;
      ttftSteps += timing.steps;
    }
  }

  return {
    turns,
    steps,
    llmMs,
    toolMs,
    ttftMs: ttftSteps > 0 ? ttftWeightedSum / ttftSteps : null,
    ttftSteps,
    decodeMs,
    decodeTokens,
    hasTiming: turns > 0,
  };
}

/**
 * 聚合整段会话的 token 账单。
 *
 * `contextTokens` 是**该轮最后一次请求**的 prompt 长度：一轮里有工具调用时，前面的
 * 请求会被后续请求覆盖，所以它不是「这一轮所有请求的 prompt 之和」。这是本项目能拿到
 * 的最细粒度，跨轮求和得到的是「每轮最后一次请求的 prompt 总量」——比「只取最后一轮」
 * 更接近账单，但仍不是上游的精确计费数（上游会把中间几次请求也计费）。弹层用词按这个
 * 口径写（「累计输入」），不假装是账单。
 */
export function sumSessionTokens(messages: readonly StatsMessageLike[]): SessionTokenTotals {
  let uncachedInputTokens = 0;
  let cacheReadTokens = 0;
  let outputTokens = 0;
  let turns = 0;

  for (const message of messages) {
    const usage = readAssistantUsage(message);
    if (!usage) continue;
    const prompt = Math.max(0, Math.round(usage.contextTokens));
    // 命中部分不会超过 prompt；上游回报写坏时夹住，避免算出负的「未缓存」
    const cached = Math.min(Math.max(0, Math.round(usage.cacheReadTokens)), prompt);
    uncachedInputTokens += prompt - cached;
    cacheReadTokens += cached;
    outputTokens += Math.max(0, Math.round(usage.outputTokens));
    turns += 1;
  }

  const billedInputTokens = uncachedInputTokens + cacheReadTokens;
  return {
    uncachedInputTokens,
    cacheReadTokens,
    cacheWriteTokens: null,
    outputTokens,
    billedInputTokens,
    totalTokens: billedInputTokens + outputTokens,
    turns,
  };
}

/** 一次遍历同时拿到计时与账单：组件里少一次全量扫描。 */
export function summarizeSession(messages: readonly StatsMessageLike[]): {
  stats: SessionStats;
  tokens: SessionTokenTotals;
} {
  return { stats: aggregateSessionStats(messages), tokens: sumSessionTokens(messages) };
}

/**
 * 会话级输出速度（tok/s）；没有解码样本时为 null。
 *
 * 与 `usage-timing.decodeTokensPerSecond` 同口径（解码 token / 解码耗时），只是分子
 * 分母已经是跨轮聚合过的量。两边都不拿整轮墙钟当分母。
 */
export function sessionTokensPerSecond(stats: SessionStats): number | null {
  if (stats.decodeMs <= 0 || stats.decodeTokens <= 0) return null;
  const timing: UsageTiming = {
    steps: stats.turns,
    turnMs: stats.llmMs + stats.toolMs,
    llmMs: stats.llmMs,
    toolMs: stats.toolMs,
    ttftMs: stats.ttftMs,
    decodeMs: stats.decodeMs,
    decodeTokens: stats.decodeTokens,
  };
  return decodeTokensPerSecond(timing);
}

/**
 * 紧凑时长：`45.2秒`（不足 1 分钟）/ `2分42秒`（1 分钟以上）。
 *
 * 与 DSH 的 `duration.compactSeconds` / `duration.compactMinutes` 同规则。秒数保留
 * 一位小数（`Math.round(s*10)/10`）：`45.0` 会显示成 `45秒`，不写多余的 `.0`。
 */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return '—';
  const seconds = ms / 1000;
  if (seconds < 60) return `${Math.round(seconds * 10) / 10}秒`;
  const whole = Math.round(seconds);
  return `${Math.floor(whole / 60)}分${whole % 60}秒`;
}

/**
 * 输出速度展示：10 tok/s 以上取整，以下保留一位小数。
 *
 * 慢的时候（比如 3 tok/s）取整会丢掉一半信息；快的时候小数没有意义。
 */
export function formatTokensPerSecond(tps: number): string {
  if (!Number.isFinite(tps) || tps < 0) return '—';
  const rounded = tps >= 10 ? Math.round(tps) : Math.round(tps * 10) / 10;
  return `${rounded} tok/s`;
}

/**
 * 千位分隔的精确 token 数：`1,234,567`。
 *
 * 不缩写：弹层是用户点开来看「到底多少」的地方，这里给 M/k 等于把精度丢在他最想要的
 * 地方。缩写留给药丸（`formatCompactTokens`）。
 */
export function formatExactTokens(value: number): string {
  if (!Number.isFinite(value)) return '—';
  const rounded = Math.round(value);
  const negative = rounded < 0;
  const digits = String(Math.abs(rounded));
  const groups: string[] = [];
  for (let end = digits.length; end > 0; end -= 3) {
    groups.unshift(digits.slice(Math.max(0, end - 3), end));
  }
  return `${negative ? '-' : ''}${groups.join(',')}`;
}

/**
 * 缩写 token 数（药丸用）：`517` / `12.2k` / `208k` / `1.7M`。
 *
 * 与 DSH 的 `formatTokens` 同规则：千位以下原样；k/M 量级里，数值 ≥100 取整
 * （`208k`），<100 保留一位小数（`1.7M`、`12.2k`）。
 */
export function formatCompactTokens(value: number): string {
  if (!Number.isFinite(value)) return '—';
  const scaled = (candidate: number): string =>
    candidate >= 100
      ? String(Math.round(candidate))
      : String(Math.round(candidate * 10) / 10);
  if (value < 0) return String(Math.round(value));
  if (value < 1_000) return String(Math.round(value));
  if (value < 1_000_000) return `${scaled(value / 1_000)}k`;
  return `${scaled(value / 1_000_000)}M`;
}

/**
 * 缓存命中率（百分数，0–100）。
 *
 * 分母是三个 prompt 侧桶之和，不是 contextTokens（见文件头）。返回整数百分比；**部分
 * 命中绝不四舍五入成 100**——「缓存命中 100%」会被读成「一个 token 都没新计费」，而
 * 实际上还有一段没命中。接近 100 的部分命中往下退到一位小数（`99.9`），仍然到 100
 * 就停在 `99.9`。
 *
 * 没有 prompt 输入（分母 0）时返回 `null`：这时「命中率」没有意义，不该显示 0%。
 */
export function cacheHitPercent(tokens: SessionTokenTotals): number | null {
  const denominator = tokens.billedInputTokens;
  if (!Number.isFinite(denominator) || denominator <= 0) return null;
  const cacheRead = Math.min(Math.max(tokens.cacheReadTokens, 0), denominator);
  if (cacheRead === denominator) return 100;
  const exact = (cacheRead / denominator) * 100;
  const rounded = Math.round(exact);
  if (rounded < 100) return rounded;
  // 部分命中却四舍五入到 100：退到一位小数，仍然不能是 100
  return Math.min(99.9, Math.floor(exact * 10) / 10);
}

/**
 * 药丸里「缓存命中 96%」那一段的百分数文本。
 *
 * 整数原样输出（`96%`），带一位小数时也原样（`99.9%`）；`null` 时返回 null，调用方
 * 据此少渲染这一段。
 */
export function formatCacheHitPercent(percent: number | null): string | null {
  if (percent === null || !Number.isFinite(percent)) return null;
  if (Number.isInteger(percent)) return `${percent}%`;
  return `${(Math.round(percent * 10) / 10).toFixed(1)}%`;
}
