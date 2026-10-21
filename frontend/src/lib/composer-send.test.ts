import { describe, expect, it } from 'vitest';

import { resolveSendGate } from './composer-send';

const gate = (overrides: Partial<Parameters<typeof resolveSendGate>[0]> = {}) =>
  resolveSendGate({ contextLocked: false, hasAttachments: false, isEmpty: false, ...overrides });

describe('resolveSendGate', () => {
  it('有内容且无阻塞时可发送', () => {
    expect(gate()).toEqual({ disabled: false, tooltip: '发送消息' });
  });

  it('空输入禁用发送，并提示先输入', () => {
    expect(gate({ isEmpty: true })).toEqual({ disabled: true, tooltip: '输入内容后发送' });
  });

  it('附件阻塞优先于空输入：告诉用户移除附件而不是催他打字', () => {
    expect(gate({ hasAttachments: true, isEmpty: true })).toEqual({
      disabled: true,
      tooltip: '附件暂不支持，请先移除',
    });
  });

  it('上下文锁优先于附件与空输入', () => {
    expect(gate({ contextLocked: true, hasAttachments: true, isEmpty: true })).toEqual({
      disabled: true,
      tooltip: '本会话上下文已满，请新建会话',
    });
  });
});
