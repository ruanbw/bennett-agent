import { describe, expect, it } from 'vitest';

import { FOLLOW_THRESHOLD_PX, isNearScrollBottom } from './scroll-follow';

const at = (scrollTop: number, scrollHeight = 1000, clientHeight = 200) => ({
  scrollTop,
  scrollHeight,
  clientHeight,
});

describe('isNearScrollBottom', () => {
  it('正好在底部时视为贴底', () => {
    expect(isNearScrollBottom(at(800))).toBe(true);
  });

  it('容差以内仍算贴底，超出就停止跟随', () => {
    expect(isNearScrollBottom(at(800 - FOLLOW_THRESHOLD_PX))).toBe(true);
    expect(isNearScrollBottom(at(800 - FOLLOW_THRESHOLD_PX - 1))).toBe(false);
  });

  it('内容不满一屏时不存在「往上翻」，始终贴底', () => {
    expect(isNearScrollBottom({ scrollTop: 0, scrollHeight: 120, clientHeight: 200 })).toBe(true);
  });

  it('顶部远距离滚动不会误判为贴底', () => {
    expect(isNearScrollBottom(at(0))).toBe(false);
  });
});
