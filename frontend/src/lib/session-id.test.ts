import { describe, expect, it } from 'vitest';

import { SESSION_ID_PATTERN, isSessionIdSendable } from './session-id';

/**
 * 会话标识的线上形状。
 *
 * 这里钉住的是**契约**而不是实现细节：后端聊天/沙箱接口用同一个 pattern 校验，两边
 * 一旦分叉，表现就是「同步接口能存的会话，聊天一发就 422」。
 */
describe('会话标识', () => {
  it('pattern 只有一处定义（聊天 / 心跳 / 状态查询共用）', () => {
    expect(SESSION_ID_PATTERN.source).toBe('^sess_[A-Za-z0-9_-]{1,59}$');
  });

  it('接受当前方案生成的 id', () => {
    expect(isSessionIdSendable('sess_0123456789abcdef0123456789abcdef')).toBe(true);
  });

  it('接受旧格式（含下划线）：同步接口一直认它，聊天不能因为它把整轮对话打挂', () => {
    expect(isSessionIdSendable('sess_1729000000000_ab12cd34')).toBe(true);
  });

  it('拒绝空值、错前缀、非法字符与超长', () => {
    expect(isSessionIdSendable('')).toBe(false);
    expect(isSessionIdSendable('msg_abcdef')).toBe(false);
    expect(isSessionIdSendable('sess_')).toBe(false);
    expect(isSessionIdSendable('sess_abc def')).toBe(false);
    expect(isSessionIdSendable('sess_中文标识')).toBe(false);
    expect(isSessionIdSendable(`sess_${'a'.repeat(60)}`)).toBe(false);
    expect(isSessionIdSendable(`sess_${'a'.repeat(59)}`)).toBe(true);
  });
});
