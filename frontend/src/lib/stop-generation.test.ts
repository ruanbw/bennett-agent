import { describe, expect, it } from 'vitest';

import { shouldStopOnEscape } from './stop-generation';

describe('shouldStopOnEscape', () => {
  it('生成中按 Esc 才停止', () => {
    expect(shouldStopOnEscape({ isRunning: true })).toBe(true);
    expect(shouldStopOnEscape({ isRunning: false })).toBe(false);
  });

  it('输入法组合中的 Esc 是取消候选词，不能顺手停掉整轮回答', () => {
    expect(shouldStopOnEscape({ isRunning: true, isComposing: true })).toBe(false);
  });

  it('弹层（设置/确认框/菜单）打开时 Esc 属于弹层', () => {
    expect(shouldStopOnEscape({ isRunning: true, hasOpenOverlay: true })).toBe(false);
    expect(shouldStopOnEscape({ isRunning: true, inModal: true })).toBe(false);
  });

  it('已经被别人 preventDefault 的按键不再重复处理', () => {
    expect(shouldStopOnEscape({ isRunning: true, defaultPrevented: true })).toBe(false);
  });
});
