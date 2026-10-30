import { describe, expect, it } from 'vitest';

import { SyncHttpError } from './api';

const error = (status: number, code: string | null = null, retryAfterSeconds: number | null = null) =>
  new SyncHttpError(`HTTP ${status}`, status, code, retryAfterSeconds);

describe('SyncHttpError 分类', () => {
  it('429 是暂时限流，不是 fatal：同步应继续退避重试', () => {
    const limited = error(429, null, 17);

    expect(limited.isFatal).toBe(false);
    expect(limited.retryAfterSeconds).toBe(17);
  });

  it('普通 4xx 仍是 fatal：重试只会撞同一面墙', () => {
    expect(error(400).isFatal).toBe(true);
    expect(error(422).isFatal).toBe(true);
    expect(error(404).isFatal).toBe(true);
  });

  it('游标失效不是 fatal：调用方会丢弃游标重拉', () => {
    expect(error(410).isFatal).toBe(false);
    expect(error(400, 'invalid_cursor').isFatal).toBe(false);
    expect(error(400, 'invalid_cursor').isCursorReset).toBe(true);
  });

  it('5xx 不是 fatal：与网络错误一样可重试', () => {
    expect(error(500).isFatal).toBe(false);
    expect(error(503).isFatal).toBe(false);
  });

  /**
   * 413 是「这一批装得太多」，不是「请求写错了」。
   *
   * 判成 fatal 等于该账号的推送永久停摆：调度器不再排程，而重试会重新组装出同一批。
   */
  it('413 不是 fatal，且单独可识别（客户端据此砍半重试）', () => {
    expect(error(413).isFatal).toBe(false);
    expect(error(413).isTooLarge).toBe(true);
    expect(error(400).isTooLarge).toBe(false);
  });
});
