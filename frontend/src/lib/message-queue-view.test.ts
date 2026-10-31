import { describe, expect, it } from 'vitest';

import { hasQueuedMessages, queueItemPreview } from './message-queue-view';

describe('hasQueuedMessages', () => {
  it('空队列不显示', () => {
    expect(hasQueuedMessages([])).toBe(false);
  });

  it('有排队消息就显示，无论几条', () => {
    expect(hasQueuedMessages([{}])).toBe(true);
    expect(hasQueuedMessages([{}, {}])).toBe(true);
  });
});

describe('queueItemPreview', () => {
  it('短文本原样返回', () => {
    expect(queueItemPreview('帮我总结这份文档')).toBe('帮我总结这份文档');
  });

  it('压平连续空白：换行与多空格折叠成单空格', () => {
    expect(queueItemPreview('第一行\n\n第二行   三列')).toBe('第一行 第二行 三列');
  });

  it('首尾空白裁掉', () => {
    expect(queueItemPreview('  hi  ')).toBe('hi');
  });

  it('超长截断并留省略号，总长不超过 maxChars', () => {
    const long = 'a'.repeat(100);
    const preview = queueItemPreview(long);
    expect(preview.length).toBe(60);
    expect(preview.endsWith('…')).toBe(true);
  });

  it('自定义上限生效', () => {
    expect(queueItemPreview('abcdef', 4)).toBe('abc…');
  });

  it('上限小于 1 时不产生负切片', () => {
    expect(queueItemPreview('abcdef', 0)).toBe('…');
  });
});
