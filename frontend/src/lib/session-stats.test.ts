import { describe, expect, it } from 'vitest';

import {
  EMPTY_SESSION_STATS,
  EMPTY_SESSION_TOKENS,
  aggregateSessionStats,
  cacheHitPercent,
  formatCacheHitPercent,
  formatCompactTokens,
  formatDuration,
  formatExactTokens,
  formatTokensPerSecond,
  sessionTokensPerSecond,
  sumSessionTokens,
  summarizeSession,
  type StatsMessageLike,
} from './session-stats';
import type { UsageTiming } from '../types';

/** 一条带计时与用量的助手消息。 */
const assistant = (
  custom: Record<string, unknown> | undefined,
  role: string = 'assistant',
): StatsMessageLike => ({ role, metadata: { custom } });

const timing = (overrides: Partial<UsageTiming> = {}): UsageTiming => ({
  steps: 3,
  turnMs: 9_000,
  llmMs: 6_000,
  toolMs: 2_000,
  ttftMs: 400,
  decodeMs: 2_000,
  decodeTokens: 400,
  ...overrides,
});

const usage = (overrides: Record<string, unknown> = {}): Record<string, unknown> => ({
  contextTokens: 2_000,
  contextWindow: 128_000,
  contextLimitTokens: 100_000,
  outputTokens: 500,
  cacheReadTokens: 1_600,
  ...overrides,
});

describe('aggregateSessionStats', () => {
  it('多轮直接累加：步数、模型用时、工具用时、解码量与解码 token', () => {
    const stats = aggregateSessionStats([
      assistant({ usage: usage({ timing: timing() }) }),
      assistant({
        usage: usage({
          timing: timing({ steps: 2, llmMs: 3_000, toolMs: 500, decodeMs: 1_000, decodeTokens: 200 }),
        }),
      }),
    ]);

    expect(stats.turns).toBe(2);
    expect(stats.steps).toBe(5);
    expect(stats.llmMs).toBe(9_000);
    expect(stats.toolMs).toBe(2_500);
    expect(stats.decodeMs).toBe(3_000);
    expect(stats.decodeTokens).toBe(600);
    expect(stats.hasTiming).toBe(true);
  });

  it('首字延迟按步数加权平均（1 步的轮不能与 10 步的轮等权）', () => {
    // 1 步 1000ms、3 步 200ms → (1000*1 + 200*3) / 4 = 400
    const stats = aggregateSessionStats([
      assistant({ usage: usage({ timing: timing({ steps: 1, ttftMs: 1_000 }) }) }),
      assistant({ usage: usage({ timing: timing({ steps: 3, ttftMs: 200, decodeMs: 0, decodeTokens: 0 }) }) }),
    ]);

    expect(stats.ttftMs).toBeCloseTo(400, 5);
    expect(stats.ttftSteps).toBe(4);
  });

  it('没有计时数据的轮次不进轮次/步数统计（否则会出现「5 轮 2 步」）', () => {
    const stats = aggregateSessionStats([
      assistant({ usage: usage({ timing: timing() }) }),
      // 老记录：有 usage 但没有 timing
      assistant({ usage: usage() }),
      assistant({ usage: usage({ timing: null }) }),
    ]);

    expect(stats.turns).toBe(1);
    expect(stats.steps).toBe(3);
    expect(stats.hasTiming).toBe(true);
  });

  it('整段会话都没有计时时 hasTiming 为 false，各项是 0 而不是编造的值', () => {
    const stats = aggregateSessionStats([assistant({ usage: usage() })]);

    expect(stats.hasTiming).toBe(false);
    expect(stats.turns).toBe(0);
    expect(stats.steps).toBe(0);
    expect(stats.ttftMs).toBeNull();
    expect(stats).toEqual(EMPTY_SESSION_STATS);
  });

  it('写坏的计时块整块忽略，而不是抛错或补 0', () => {
    const stats = aggregateSessionStats([
      assistant({ usage: usage({ timing: { steps: 'x', turnMs: 1 } }) }),
      assistant({ usage: usage({ timing: timing({ steps: 2 }) }) }),
    ]);

    expect(stats.turns).toBe(1);
    expect(stats.steps).toBe(2);
  });

  it('用户消息、缺 metadata、缺 custom 都被忽略', () => {
    const stats = aggregateSessionStats([
      assistant({ usage: usage({ timing: timing() }) }, 'user'),
      { role: 'assistant' },
      { role: 'assistant', metadata: {} },
      { role: 'assistant', metadata: { custom: undefined } },
    ]);

    expect(stats).toEqual(EMPTY_SESSION_STATS);
  });

  it('没有首字样本时 ttftMs 为 null，而不是 0（「首字 0ms」会被读成秒回）', () => {
    const stats = aggregateSessionStats([
      assistant({ usage: usage({ timing: timing({ ttftMs: null }) }) }),
    ]);

    expect(stats.ttftMs).toBeNull();
    expect(stats.ttftSteps).toBe(0);
    expect(stats.hasTiming).toBe(true);
  });
});

describe('sumSessionTokens', () => {
  it('按三个 prompt 侧桶累加，未缓存部分是推导值', () => {
    const tokens = sumSessionTokens([
      assistant({ usage: usage({ contextTokens: 2_000, cacheReadTokens: 1_600, outputTokens: 500 }) }),
      assistant({ usage: usage({ contextTokens: 4_000, cacheReadTokens: 3_000, outputTokens: 100 }) }),
    ]);

    expect(tokens.uncachedInputTokens).toBe(400 + 1_000);
    expect(tokens.cacheReadTokens).toBe(4_600);
    expect(tokens.billedInputTokens).toBe(6_000);
    expect(tokens.outputTokens).toBe(600);
    expect(tokens.totalTokens).toBe(6_600);
    expect(tokens.turns).toBe(2);
  });

  it('缓存写入未知保持 null（不是 0：0 表示「确实没写」）', () => {
    const tokens = sumSessionTokens([assistant({ usage: usage() })]);

    expect(tokens.cacheWriteTokens).toBeNull();
  });

  it('命中数写坏（超过 prompt）时夹住，不产生负的未缓存输入', () => {
    const tokens = sumSessionTokens([
      assistant({ usage: usage({ contextTokens: 1_000, cacheReadTokens: 9_999 }) }),
    ]);

    expect(tokens.cacheReadTokens).toBe(1_000);
    expect(tokens.uncachedInputTokens).toBe(0);
    expect(tokens.billedInputTokens).toBe(1_000);
  });

  it('没有 usage 的会话是全 0 的空账单', () => {
    expect(sumSessionTokens([])).toEqual(EMPTY_SESSION_TOKENS);
    expect(sumSessionTokens([{ role: 'user' }])).toEqual(EMPTY_SESSION_TOKENS);
  });
});

describe('summarizeSession', () => {
  it('一次遍历同时给出计时与账单', () => {
    const { stats, tokens } = summarizeSession([
      assistant({ usage: usage({ timing: timing() }) }),
    ]);

    expect(stats.turns).toBe(1);
    expect(tokens.turns).toBe(1);
    expect(tokens.totalTokens).toBe(tokens.billedInputTokens + tokens.outputTokens);
  });
});

describe('sessionTokensPerSecond', () => {
  it('用解码阶段的 token 除以解码阶段耗时，而不是整轮墙钟', () => {
    const stats = aggregateSessionStats([
      assistant({ usage: usage({ timing: timing({ decodeMs: 2_000, decodeTokens: 400 }) }) }),
    ]);

    // 400 tok / 2s = 200 tok/s；若误用 turnMs(9s) 会得到 44.4
    expect(sessionTokensPerSecond(stats)).toBeCloseTo(200, 5);
  });

  it('没有解码样本时返回 null（不显示 0 tok/s）', () => {
    const stats = aggregateSessionStats([
      assistant({ usage: usage({ timing: timing({ decodeMs: null, decodeTokens: null }) }) }),
    ]);

    expect(sessionTokensPerSecond(stats)).toBeNull();
    expect(sessionTokensPerSecond(EMPTY_SESSION_STATS)).toBeNull();
  });

  it('解码耗时为 0 时同样没有速度（避免 Infinity）', () => {
    const stats = aggregateSessionStats([
      assistant({ usage: usage({ timing: timing({ decodeMs: 0, decodeTokens: 10 }) }) }),
    ]);

    expect(sessionTokensPerSecond(stats)).toBeNull();
  });
});

describe('formatDuration', () => {
  it('不足一分钟给一位小数的秒', () => {
    expect(formatDuration(45_230)).toBe('45.2秒');
    expect(formatDuration(1_000)).toBe('1秒');
    expect(formatDuration(0)).toBe('0秒');
  });

  it('一分钟以上给「M分S秒」', () => {
    expect(formatDuration(162_000)).toBe('2分42秒');
    expect(formatDuration(60_000)).toBe('1分0秒');
  });

  it('非法数字给占位符，而不是 NaN秒', () => {
    expect(formatDuration(Number.NaN)).toBe('—');
    expect(formatDuration(-1)).toBe('—');
  });
});

describe('formatTokensPerSecond', () => {
  it('两位数以上取整，慢的时候保留一位小数', () => {
    expect(formatTokensPerSecond(208.4)).toBe('208 tok/s');
    expect(formatTokensPerSecond(9.87)).toBe('9.9 tok/s');
    expect(formatTokensPerSecond(3)).toBe('3 tok/s');
  });

  it('非法数字给占位符', () => {
    expect(formatTokensPerSecond(Number.NaN)).toBe('—');
    expect(formatTokensPerSecond(-5)).toBe('—');
  });
});

describe('formatExactTokens', () => {
  it('千位分隔，不缩写', () => {
    expect(formatExactTokens(0)).toBe('0');
    expect(formatExactTokens(999)).toBe('999');
    expect(formatExactTokens(1_234_567)).toBe('1,234,567');
    expect(formatExactTokens(1_000)).toBe('1,000');
  });

  it('非法数字给占位符', () => {
    expect(formatExactTokens(Number.NaN)).toBe('—');
  });
});

describe('formatCompactTokens', () => {
  it('按 DSH 规则缩写：千位以下原样，k/M 量级大数取整、小数保留一位', () => {
    expect(formatCompactTokens(517)).toBe('517');
    expect(formatCompactTokens(12_200)).toBe('12.2k');
    expect(formatCompactTokens(208_000)).toBe('208k');
    expect(formatCompactTokens(1_700_000)).toBe('1.7M');
    expect(formatCompactTokens(999)).toBe('999');
    expect(formatCompactTokens(1_000_000)).toBe('1M');
  });

  it('非法数字给占位符', () => {
    expect(formatCompactTokens(Number.NaN)).toBe('—');
  });
});

describe('cacheHitPercent', () => {
  const tokens = (overrides: Partial<typeof EMPTY_SESSION_TOKENS> = {}) => {
    const base = { ...EMPTY_SESSION_TOKENS, turns: 1, ...overrides };
    return { ...base, billedInputTokens: base.uncachedInputTokens + base.cacheReadTokens };
  };

  it('分母是三个 prompt 侧桶之和，不是 contextTokens', () => {
    // 命中 1,600 / 计费输入 2,000 = 80%
    expect(cacheHitPercent(tokens({ cacheReadTokens: 1_600, uncachedInputTokens: 400 }))).toBe(80);
  });

  it('没有计费输入时返回 null（不显示 0%）', () => {
    expect(cacheHitPercent(tokens())).toBeNull();
  });

  it('全部命中时是 100', () => {
    expect(cacheHitPercent(tokens({ cacheReadTokens: 2_000, uncachedInputTokens: 0 }))).toBe(100);
  });

  it('部分命中绝不四舍五入成 100（那会被读成「一个 token 都没新计费」）', () => {
    // 9999/10000 = 99.99% → 普通取整会变 100
    expect(cacheHitPercent(tokens({ cacheReadTokens: 9_999, uncachedInputTokens: 1 }))).toBe(99.9);
  });

  it('普通情况取整数百分比', () => {
    expect(cacheHitPercent(tokens({ cacheReadTokens: 1, uncachedInputTokens: 2 }))).toBe(33);
  });
});

describe('formatCacheHitPercent', () => {
  it('整数原样，带小数补一位', () => {
    expect(formatCacheHitPercent(96)).toBe('96%');
    expect(formatCacheHitPercent(99.9)).toBe('99.9%');
    expect(formatCacheHitPercent(100)).toBe('100%');
  });

  it('null 时返回 null，调用方据此少渲染这一段', () => {
    expect(formatCacheHitPercent(null)).toBeNull();
    expect(formatCacheHitPercent(Number.NaN)).toBeNull();
  });
});
