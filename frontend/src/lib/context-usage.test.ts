import { describe, expect, it } from 'vitest';

import {
  describeContextUsage,
  estimateRemainingChars,
  formatPercent,
  formatTokens,
  readUsage,
} from './context-usage';
import type { UsageState } from '../types';

const usage = (overrides: Partial<UsageState> = {}): UsageState => ({
  contextTokens: 851,
  contextWindow: 1_000_000,
  contextLimitTokens: null,
  outputTokens: 123,
  cacheReadTokens: 0,
  ...overrides,
});

describe('formatTokens', () => {
  it('小数值用千位分隔，大数值缩写成 k/M', () => {
    expect(formatTokens(0)).toBe('0');
    expect(formatTokens(999)).toBe('999');
    expect(formatTokens(1066)).toBe('1,066');
    expect(formatTokens(9999)).toBe('9,999');
    expect(formatTokens(12_000)).toBe('12k');
    expect(formatTokens(1_000_000)).toBe('1.0M');
    expect(formatTokens(1_250_000)).toBe('1.3M');
  });

  it('非法数字不猜，直接给占位符', () => {
    expect(formatTokens(Number.NaN)).toBe('—');
    expect(formatTokens(-1)).toBe('—');
  });
});

describe('formatPercent', () => {
  it('保留一位小数', () => {
    expect(formatPercent(12.34)).toBe('12.3%');
    expect(formatPercent(0)).toBe('0.0%');
    expect(formatPercent(100)).toBe('100.0%');
  });

  it('「很小但不是零」写成 <0.1%，而不是看起来像没用量', () => {
    expect(formatPercent(0.03)).toBe('<0.1%');
    expect(formatPercent(0.1)).toBe('0.1%');
  });

  it('超出 0–100 的值被夹住（上游回报离谱时也不显示 123.4%）', () => {
    expect(formatPercent(123.4)).toBe('100.0%');
    expect(formatPercent(-5)).toBe('0.0%');
    expect(formatPercent(Number.NaN)).toBe('—');
  });
});

describe('readUsage', () => {
  it('结构完整时原样取用', () => {
    expect(readUsage(usage())).toEqual(usage());
  });

  it('缺少 contextTokens 或不是对象时当作没有（老记录 / 写坏的数据）', () => {
    expect(readUsage(null)).toBeNull();
    expect(readUsage(undefined)).toBeNull();
    expect(readUsage('851')).toBeNull();
    expect(readUsage({ contextWindow: 1000 })).toBeNull();
    expect(readUsage({ contextTokens: Number.NaN })).toBeNull();
  });

  it('可选字段缺失时补 0 / null，而不是让后面算占比时炸掉', () => {
    expect(readUsage({ contextTokens: 20 })).toEqual({
      contextTokens: 20,
      contextWindow: null,
      contextLimitTokens: null,
      outputTokens: 0,
      cacheReadTokens: 0,
    });
  });
});

describe('describeContextUsage', () => {
  it('没有用量就没有指示器', () => {
    expect(describeContextUsage(null)).toBeNull();
    expect(describeContextUsage(undefined)).toBeNull();
  });

  it('窗口与占比来自真实用量', () => {
    const view = describeContextUsage(usage(), { currentModel: 'auto/x', usageModel: 'auto/x' })!;

    expect(view.contextTokens).toBe(851);
    expect(view.contextWindow).toBe(1_000_000);
    expect(view.windowSource).toBe('reported');
    expect(view.limitTokens).toBe(1_000_000);
    expect(view.limitSource).toBe('window');
    expect(view.usedPercent).toBeCloseTo(0.0851, 4);
    expect(view.remainingTokens).toBe(999_149);
    expect(view.remainingPercent).toBeCloseTo(99.9149, 3);
    expect(view.compactLabel).toBe('851 / 1.0M');
    expect(view.summary).toBe('上下文 851 / 1.0M（<0.1%）');
  });

  it('预算上限优先于原始窗口：后端按「窗口 - 回答预留」判定，前端必须同一口径', () => {
    // 32k 窗口、18k 可用上限：851 tokens 在窗口口径下是 2.7%，在预算口径下是 4.7%。
    // 测试更极端的占比：17.5k 在窗口口径下 54.7%（看起来还很宽敞），
    // 预算口径 97.2%（下一轮随时会被拒）。
    const view = describeContextUsage(
      usage({ contextWindow: 32_000, contextLimitTokens: 18_000, contextTokens: 17_500 }),
      { currentModel: 'auto/x', usageModel: 'auto/x', modelContextWindow: 32_000 },
    )!;

    expect(view.limitTokens).toBe(18_000);
    expect(view.limitSource).toBe('budget');
    expect(view.contextWindow).toBe(32_000);
    expect(view.usedPercent).toBeCloseTo((17_500 / 18_000) * 100, 6);
    expect(view.remainingTokens).toBe(500);
    expect(view.compactLabel).toBe('17.5k / 18k');
    expect(view.summary).toContain('可用上限 18k');
    expect(view.summary).toContain('模型窗口 32k');
  });

  it('切模型后丢掉旧模型的预算上限（不能拿旧余量给新模型算占比）', () => {
    const view = describeContextUsage(
      usage({ contextWindow: 1_000_000, contextLimitTokens: 18_000 }),
      { currentModel: 'auto/new', usageModel: 'auto/old', modelContextWindow: 32_000 },
    )!;

    expect(view.limitTokens).toBe(32_000);
    expect(view.limitSource).toBe('window');
    expect(view.usedPercent).toBeCloseTo((851 / 32_000) * 100, 6);
  });

  it('切模型且新窗口未知时，旧预算与新窗口都不可用，只报绝对值', () => {
    const view = describeContextUsage(
      usage({ contextWindow: 1_000_000, contextLimitTokens: 18_000 }),
      { currentModel: 'auto/new', usageModel: 'auto/old', modelContextWindow: null },
    )!;

    expect(view.limitTokens).toBeNull();
    expect(view.limitSource).toBeNull();
    expect(view.usedPercent).toBeNull();
    expect(view.compactLabel).toBe('851');
  });

  it('拿不到预算时，当前模型窗口已知就按它算（而不是上游回报的旧窗口）', () => {
    const view = describeContextUsage(usage({ contextWindow: 1_000_000 }), {
      currentModel: 'auto/x',
      usageModel: 'auto/x',
      modelContextWindow: 32_000,
    })!;

    expect(view.contextWindow).toBe(32_000);
    expect(view.windowSource).toBe('model');
    expect(view.usedPercent).toBeCloseTo(2.66, 2);
  });

  it('切模型后不再拿旧模型的窗口算占比（否则会给出错误的安全感）', () => {
    const view = describeContextUsage(usage({ contextWindow: 1_000_000 }), {
      currentModel: 'auto/new',
      usageModel: 'auto/old',
      modelContextWindow: null,
    })!;

    expect(view.contextWindow).toBeNull();
    expect(view.usedPercent).toBeNull();
    expect(view.remainingTokens).toBeNull();
    // 窗口未知时只报绝对值，不编造百分比
    expect(view.compactLabel).toBe('851');
    expect(view.summary).toBe('上下文 851 tokens（窗口未知）');
  });

  it('切到一个窗口已知的模型时按新窗口算占比', () => {
    const view = describeContextUsage(usage({ contextWindow: 1_000_000 }), {
      currentModel: 'auto/new',
      usageModel: 'auto/old',
      modelContextWindow: 8_000,
    })!;

    expect(view.contextWindow).toBe(8_000);
    expect(view.usedPercent).toBeCloseTo(10.6375, 3);
    expect(view.compactLabel).toBe('851 / 8,000');
  });

  it('超过窗口时占比夹在 100、剩余不为负', () => {
    const view = describeContextUsage(usage({ contextTokens: 9_000 }), {
      currentModel: 'auto/x',
      usageModel: 'auto/x',
      modelContextWindow: 8_000,
    })!;

    expect(view.usedPercent).toBe(100);
    expect(view.remainingTokens).toBe(0);
    expect(view.remainingPercent).toBe(0);
  });

  it('缓存率是「命中部分占已用上下文」的比例，且不会超过 100%', () => {
    const hit = describeContextUsage(usage({ cacheReadTokens: 640 }), { currentModel: 'm', usageModel: 'm' })!;
    expect(hit.cachePercent).toBeCloseTo((640 / 851) * 100, 6);

    const bogus = describeContextUsage(usage({ cacheReadTokens: 9_999 }), { currentModel: 'm', usageModel: 'm' })!;
    expect(bogus.cachePercent).toBe(100);
  });

  it('模型 id 都不知道（老记录）时退回上游回报的窗口', () => {
    const view = describeContextUsage(usage(), { currentModel: null, usageModel: null })!;
    expect(view.contextWindow).toBe(1_000_000);
    expect(view.windowSource).toBe('reported');
  });

  it('上游也没报窗口时只报绝对值', () => {
    const view = describeContextUsage(usage({ contextWindow: null }), { currentModel: 'm', usageModel: 'm' })!;
    expect(view.contextWindow).toBeNull();
    expect(view.windowSource).toBeNull();
    expect(view.compactLabel).toBe('851');
    expect(view.cachePercent).toBe(0);
  });

  it('窗口为 0 / 负数时按「没报」处理（不除以 0）', () => {
    const view = describeContextUsage(usage({ contextWindow: 0 }), { currentModel: 'm', usageModel: 'm' })!;
    expect(view.contextWindow).toBeNull();
    expect(view.usedPercent).toBeNull();
  });
});

describe('estimateRemainingChars', () => {
  it('有余量时给一个量级参照（按中文 ≈1 字/token 粗估）', () => {
    expect(estimateRemainingChars(18_000)).toBe('约 18k 字');
    expect(estimateRemainingChars(850)).toBe('约 850 字');
  });

  it('未知或已无余量时不写「约 0 字」', () => {
    expect(estimateRemainingChars(null)).toBe('');
    expect(estimateRemainingChars(0)).toBe('');
    expect(estimateRemainingChars(-5)).toBe('');
    expect(estimateRemainingChars(Number.NaN)).toBe('');
  });
});
