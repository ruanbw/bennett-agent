import { describe, expect, it } from 'vitest';

import { FOLLOW_THRESHOLD_PX, isNearScrollBottom, shouldFocusComposerAfterScrollToBottom } from './scroll-follow';

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

describe('shouldFocusComposerAfterScrollToBottom', () => {
  it('桌面端滚到底后把焦点交回输入框', () => {
    expect(shouldFocusComposerAfterScrollToBottom(false)).toBe(true);
  });

  it('粗指针（触屏）不抢焦点，避免弹出软键盘挡住最新消息', () => {
    expect(shouldFocusComposerAfterScrollToBottom(true)).toBe(false);
  });
});
