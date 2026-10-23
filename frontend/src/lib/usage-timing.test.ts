import { describe, expect, it } from 'vitest';

import {
  averageFirstTokenMs,
  decodeTokensPerSecond,
  readUsageTiming,
} from './usage-timing';

const timing = (overrides: Record<string, unknown> = {}) => ({
  steps: 3,
  turnMs: 6800,
  llmMs: 4200,
  toolMs: 2400,
  ttftMs: 520,
  decodeMs: 2000,
  decodeTokens: 400,
  ...overrides,
});

describe('readUsageTiming', () => {
  it('字段齐全时原样读出并取整', () => {
    expect(readUsageTiming(timing())).toEqual({
      steps: 3,
      turnMs: 6800,
      llmMs: 4200,
      toolMs: 2400,
      ttftMs: 520,
      decodeMs: 2000,
      decodeTokens: 400,
    });
  });

  it('缺 steps 或 turnMs 就整块算没有——没有步数的「轮次统计」没有意义', () => {
    expect(readUsageTiming(timing({ steps: undefined }))).toBeNull();
    expect(readUsageTiming(timing({ turnMs: undefined }))).toBeNull();
    expect(readUsageTiming(timing({ steps: null }))).toBeNull();
    expect(readUsageTiming(timing({ turnMs: null }))).toBeNull();
  });

  it('llmMs / toolMs 缺失按 0，不影响药丸本体', () => {
    const result = readUsageTiming(timing({ llmMs: undefined, toolMs: null }));
    expect(result).not.toBeNull();
    expect(result!.llmMs).toBe(0);
    expect(result!.toolMs).toBe(0);
  });

  it('首字与解码阶段缺失时保持 null，对应读数隐藏，不补 0', () => {
    const result = readUsageTiming(
      timing({ ttftMs: null, decodeMs: undefined, decodeTokens: undefined })
    );
    expect(result).not.toBeNull();
    expect(result!.ttftMs).toBeNull();
    expect(result!.decodeMs).toBeNull();
    expect(result!.decodeTokens).toBeNull();
  });

  it('非对象、null、字段写坏都当没有，而不是抛错', () => {
    expect(readUsageTiming(null)).toBeNull();
    expect(readUsageTiming(undefined)).toBeNull();
    expect(readUsageTiming('timing')).toBeNull();
    expect(readUsageTiming(42)).toBeNull();
    expect(readUsageTiming(timing({ steps: 'three' }))).toBeNull();
    expect(readUsageTiming(timing({ turnMs: NaN }))).toBeNull();
    expect(readUsageTiming(timing({ turnMs: -1 }))).toBeNull();
  });
});

describe('decodeTokensPerSecond', () => {
  it('用解码阶段的 token 与耗时相除，而不是整轮墙钟', () => {
    // 400 tok / 2s = 200 tok/s；若误用 turnMs（6.8s）会得到约 59，差 3 倍多
    expect(decodeTokensPerSecond(readUsageTiming(timing()))).toBe(200);
  });

  it('分母为 0 或缺失时返回 null（不给 Infinity / NaN）', () => {
    expect(decodeTokensPerSecond(readUsageTiming(timing({ decodeMs: 0 })))).toBeNull();
    expect(decodeTokensPerSecond(readUsageTiming(timing({ decodeMs: null })))).toBeNull();
    expect(decodeTokensPerSecond(null)).toBeNull();
  });

  it('没有解码 token 时同样返回 null', () => {
    expect(decodeTokensPerSecond(readUsageTiming(timing({ decodeTokens: null })))).toBeNull();
  });
});

describe('averageFirstTokenMs', () => {
  it('直接透出服务端算好的步均首字延迟', () => {
    expect(averageFirstTokenMs(readUsageTiming(timing()))).toBe(520);
  });

  it('缺失时返回 null', () => {
    expect(averageFirstTokenMs(readUsageTiming(timing({ ttftMs: null })))).toBeNull();
    expect(averageFirstTokenMs(null)).toBeNull();
  });
});
