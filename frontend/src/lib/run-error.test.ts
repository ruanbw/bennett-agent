import { describe, expect, it } from 'vitest';

import { describeErrorHint, describeErrorLabel, readKnownErrorCode } from './run-error';

describe('readKnownErrorCode', () => {
  it('认三种服务端分类', () => {
    expect(readKnownErrorCode('context_overflow')).toBe('context_overflow');
    expect(readKnownErrorCode('model_unavailable')).toBe('model_unavailable');
    expect(readKnownErrorCode('internal_error')).toBe('internal_error');
  });

  it('协议外的取值与非字符串一律当未分类（不能把未知字符串带进 run）', () => {
    expect(readKnownErrorCode('something_new')).toBeNull();
    expect(readKnownErrorCode(undefined)).toBeNull();
    expect(readKnownErrorCode(null)).toBeNull();
    expect(readKnownErrorCode(42)).toBeNull();
  });
});

describe('describeErrorLabel', () => {
  it('按分类换标题：上下文已满不是「错误」，模型不可用也不是', () => {
    expect(describeErrorLabel('context_overflow')).toBe('上下文已满');
    expect(describeErrorLabel('model_unavailable')).toBe('模型不可用');
    expect(describeErrorLabel('internal_error')).toBe('服务端错误');
    expect(describeErrorLabel(null)).toBe('错误');
  });
});

describe('describeErrorHint', () => {
  it('模型不可用给出确定动作：换模型 + 刷新列表', () => {
    const hint = describeErrorHint('model_unavailable');
    expect(hint).toContain('换一个模型');
    expect(hint).toContain('刷新');
  });

  it('上下文已满说明重试无用并给出出路', () => {
    const hint = describeErrorHint('context_overflow');
    expect(hint).toContain('重试不会成功');
    expect(hint).toContain('新建会话');
  });

  it('未分类与服务端错误不给凭空的建议', () => {
    expect(describeErrorHint(null)).toBe('');
    expect(describeErrorHint('internal_error')).toBe('');
  });
});
