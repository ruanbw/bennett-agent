import { describe, expect, it } from 'vitest';

import {
  MAX_COOLDOWN_MS,
  describeCooldown,
  remainingCooldownMs,
} from './rate-limit-cooldown';

describe('remainingCooldownMs', () => {
  it('按绝对时刻算剩余时间', () => {
    expect(remainingCooldownMs(11_000, 10_000)).toBe(1_000);
    expect(remainingCooldownMs(16_500, 12_000)).toBe(4_500);
  });

  it('到点或已过点时为 0（不冷却）', () => {
    expect(remainingCooldownMs(10_000, 10_000)).toBe(0);
    expect(remainingCooldownMs(10_000, 15_000)).toBe(0);
  });

  it('缺失/写坏的输入当成不冷却，而不是抛错', () => {
    for (const value of [null, undefined, 'soon', Number.NaN, {}]) {
      expect(remainingCooldownMs(value, 10_000)).toBe(0);
    }
  });

  it('荒谬的大值封顶，免得按钮被永久禁用', () => {
    expect(remainingCooldownMs(10_000 + MAX_COOLDOWN_MS * 10, 10_000)).toBe(MAX_COOLDOWN_MS);
  });
});

describe('describeCooldown', () => {
  it('向上取整：剩 0.4 秒时说「1 秒后」而不是「0 秒后」', () => {
    expect(describeCooldown(400)).toBe('1 秒后可重试');
    expect(describeCooldown(1_000)).toBe('1 秒后可重试');
    expect(describeCooldown(11_200)).toBe('12 秒后可重试');
  });

  it('没有剩余时间时返回空串（调用方据此显示普通「重试」）', () => {
    expect(describeCooldown(0)).toBe('');
    expect(describeCooldown(-5)).toBe('');
    expect(describeCooldown(Number.NaN)).toBe('');
  });
});
