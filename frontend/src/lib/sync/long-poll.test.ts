/**
 * 长轮询超时策略的行为锁。
 *
 * 这条策略决定「偶发卡顿」和「同步真的坏了」的边界：判错一边会误报（用户以为数据没
 * 保存），另一边会把真实故障藏起来。用纯函数锁住阈值与复位语义。
 */

import { describe, expect, it } from 'vitest';

import {
  MAX_SILENT_PULL_TIMEOUTS,
  PULL_TIMEOUT_RETRY_MS,
  createTimeoutStreak,
  isPullTimeout,
} from './long-poll';

describe('isPullTimeout', () => {
  it('只认 AbortSignal.timeout 抛出的 TimeoutError', () => {
    const timeout = new Error('signal timed out');
    timeout.name = 'TimeoutError';
    expect(isPullTimeout(timeout)).toBe(true);

    expect(isPullTimeout(new TypeError('fetch failed'))).toBe(false);
    expect(isPullTimeout(new Error('boom'))).toBe(false);
    expect(isPullTimeout('TimeoutError')).toBe(false);
    expect(isPullTimeout(null)).toBe(false);
  });
});

describe('createTimeoutStreak', () => {
  it('前 N-1 次静默，第 N 次才上报', () => {
    const streak = createTimeoutStreak();
    for (let i = 1; i < MAX_SILENT_PULL_TIMEOUTS; i += 1) {
      expect(streak.record()).toBe(false);
    }
    expect(streak.record()).toBe(true);
  });

  it('到达阈值后继续上报（不做「只报一次」的降级）', () => {
    const streak = createTimeoutStreak(2);
    expect(streak.record()).toBe(false);
    expect(streak.record()).toBe(true);
    expect(streak.record()).toBe(true);
  });

  it('成功一次立即清零，下一次重新按偶发处理', () => {
    const streak = createTimeoutStreak(2);
    streak.record();
    streak.record();
    streak.reset();

    expect(streak.record()).toBe(false);
  });
});

describe('超时重试的等待时间', () => {
  it('固定短延迟，不随退避增长（退避翻倍会让冻结几次后同步慢到形同停摆）', () => {
    expect(PULL_TIMEOUT_RETRY_MS).toBeLessThanOrEqual(5_000);
  });
});
