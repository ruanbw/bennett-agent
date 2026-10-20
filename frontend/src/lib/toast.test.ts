/**
 * 提示条状态机的行为锁。
 *
 * toast 是最容易被写成「能弹出来就算完」的组件，真正难的是边界：同一条消息连点两次
 * 会不会叠两条、满了之后挤掉谁、错误是不是比成功停得久、订阅者有没有被正确通知。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  MAX_TOASTS,
  TOAST_DURATION_MS,
  dismissAllToasts,
  dismissToast,
  getToasts,
  notify,
  subscribeToasts,
} from './toast';

beforeEach(() => {
  vi.useFakeTimers();
  dismissAllToasts();
});

afterEach(() => {
  dismissAllToasts();
  vi.useRealTimers();
});

describe('notify', () => {
  it('插入一条并返回 id', () => {
    const id = notify('success', '已导出 a.md');

    expect(getToasts()).toEqual([{ id, tone: 'success', message: '已导出 a.md' }]);
  });

  it('同语气同文案去重：不叠加，只重置倒计时', () => {
    const first = notify('success', '已导出 a.md');
    vi.advanceTimersByTime(3_000);

    const second = notify('success', '已导出 a.md');
    expect(second).toBe(first);
    expect(getToasts()).toHaveLength(1);

    // 距第一次已 6s（超过 4s），但第二次报告重置了计时
    vi.advanceTimersByTime(3_000);
    expect(getToasts()).toHaveLength(1);
    vi.advanceTimersByTime(1_100);
    expect(getToasts()).toHaveLength(0);
  });

  it('语气不同不去重（同一句话的成功与失败是两件事）', () => {
    notify('success', '操作完成');
    notify('error', '操作完成');

    expect(getToasts()).toHaveLength(2);
  });

  it(`最多同时 ${MAX_TOASTS} 条，更早的被挤掉`, () => {
    notify('info', '一');
    notify('info', '二');
    notify('info', '三');
    notify('info', '四');

    expect(getToasts().map((toast) => toast.message)).toEqual(['二', '三', '四']);
  });

  it('错误比成功停得久', () => {
    notify('success', '成功');
    vi.advanceTimersByTime(TOAST_DURATION_MS.success);
    expect(getToasts()).toHaveLength(0);

    notify('error', '失败');
    vi.advanceTimersByTime(TOAST_DURATION_MS.success);
    expect(getToasts()).toHaveLength(1);
    vi.advanceTimersByTime(TOAST_DURATION_MS.error - TOAST_DURATION_MS.success);
    expect(getToasts()).toHaveLength(0);
  });
});

describe('dismiss', () => {
  it('按 id 关闭一条，其余保留', () => {
    const first = notify('info', '一');
    notify('info', '二');

    dismissToast(first);

    expect(getToasts().map((toast) => toast.message)).toEqual(['二']);
  });

  it('关闭不存在的 id 是空操作（不通知订阅者）', () => {
    const listener = vi.fn();
    const unsubscribe = subscribeToasts(listener);
    try {
      dismissToast('nope');
      expect(listener).not.toHaveBeenCalled();
    } finally {
      unsubscribe();
    }
  });

  it('订阅者收到新增/关闭通知，退订后不再收到', () => {
    const listener = vi.fn();
    const unsubscribe = subscribeToasts(listener);

    const id = notify('info', '一');
    dismissToast(id);
    expect(listener).toHaveBeenCalledTimes(2);

    unsubscribe();
    notify('info', '二');
    expect(listener).toHaveBeenCalledTimes(2);
  });

  it('dismissAllToasts 清空并取消所有计时器', () => {
    notify('error', '一');
    notify('error', '二');

    dismissAllToasts();
    expect(getToasts()).toHaveLength(0);

    // 计时器已取消：推进时间不会再来一次空通知（也不会抛错）
    vi.advanceTimersByTime(TOAST_DURATION_MS.error * 2);
    expect(getToasts()).toHaveLength(0);
  });
});
