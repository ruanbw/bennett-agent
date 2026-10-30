import { describe, expect, it } from 'vitest';

import { ChatRequestError } from '../lib/chat-request-error';
import { describeConnectionFailure } from '../lib/connection-failure';
import { SESSION_EXPIRED_MESSAGE } from './use-auth';

/**
 * 会话失效时的那句提示。
 *
 * `useAuth` 本身是 hook，而本仓库没有 jsdom、也没有能跑 effect 的 renderer，所以这里
 * 钉的是它的**文案契约**：全局 401 处理会把这句话放进 `error`，登录页的 `role="alert"`
 * 原样念出来。同一句文案必须与消息区 401 的说法一致——同一次失效在界面上有两种说法，
 * 用户会以为是两个不同的问题。
 */
describe('会话失效的提示', () => {
  it('与消息区 401 的说明逐字一致', () => {
    expect(describeConnectionFailure(new ChatRequestError(401)).message).toBe(SESSION_EXPIRED_MESSAGE);
  });

  it('说清是「失效」并给出下一步（重新登录），不是一句泛泛的失败', () => {
    expect(SESSION_EXPIRED_MESSAGE).toContain('失效');
    expect(SESSION_EXPIRED_MESSAGE).toContain('重新登录');
  });
});
