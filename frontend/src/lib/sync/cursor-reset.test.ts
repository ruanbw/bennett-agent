import { describe, expect, it } from 'vitest';

import { MAX_CURSOR_RESETS, createCursorResetGuard } from './cursor-reset';

describe('游标失效守卫', () => {
  it('第一次：丢游标并标脏（让本机数据补推上去）', () => {
    const guard = createCursorResetGuard();

    expect(guard.record()).toEqual({ action: 'resync', markAllPending: true, attempt: 1 });
  });

  it('连续失效时不再重复标脏：整库重推是这场风暴的放大器', () => {
    const guard = createCursorResetGuard();
    guard.record();

    expect(guard.record()).toEqual({ action: 'resync', markAllPending: false, attempt: 2 });
  });

  it(`超过 ${MAX_CURSOR_RESETS} 次就放弃，不再无限重拉`, () => {
    const guard = createCursorResetGuard();

    for (let index = 1; index <= MAX_CURSOR_RESETS; index += 1) {
      expect(guard.record().action).toBe('resync');
    }

    expect(guard.record()).toEqual({ action: 'giveup', markAllPending: false, attempt: MAX_CURSOR_RESETS + 1 });
  });

  it('成功一次就清零（下一次失效重新按「首次」处理）', () => {
    const guard = createCursorResetGuard();
    guard.record();
    guard.record();
    guard.reset();

    expect(guard.attempts()).toBe(0);
    expect(guard.record()).toEqual({ action: 'resync', markAllPending: true, attempt: 1 });
  });
});
