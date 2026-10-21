import { describe, expect, it } from 'vitest';

import { parseRetryAfterSeconds, retryDelayMs } from './retry-after';

describe('parseRetryAfterSeconds', () => {
  it('解析正常的秒数', () => {
    expect(parseRetryAfterSeconds('17')).toBe(17);
    expect(parseRetryAfterSeconds(' 3 ')).toBe(3);
  });

  it('缺失、非数字、HTTP-date 都返回 null', () => {
    expect(parseRetryAfterSeconds(null)).toBeNull();
    expect(parseRetryAfterSeconds('')).toBeNull();
    expect(parseRetryAfterSeconds('Wed, 21 Oct 2026 07:28:00 GMT')).toBeNull();
    expect(parseRetryAfterSeconds('soon')).toBeNull();
  });

  it('0 与负数不产生「立即重试」的误导', () => {
    expect(parseRetryAfterSeconds('0')).toBeNull();
    expect(parseRetryAfterSeconds('-5')).toBeNull();
  });
});

describe('retryDelayMs', () => {
  it('没有 Retry-After 时用客户端退避', () => {
    expect(retryDelayMs(null, 4_000, 300_000)).toBe(4_000);
  });

  it('有 Retry-After 就听服务端的，不被已经爬高的退避拖住', () => {
    expect(retryDelayMs(2, 60_000, 300_000)).toBe(2_000);
  });

  it('异常大的 Retry-After 用上限保护', () => {
    expect(retryDelayMs(3_600, 2_000, 300_000)).toBe(300_000);
  });
});
