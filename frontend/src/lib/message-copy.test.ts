import { describe, expect, it } from 'vitest';

import { messageTextForCopy } from './message-copy';

describe('messageTextForCopy', () => {
  it('只拼接文本分片，跳过思考与工具调用', () => {
    const text = messageTextForCopy([
      { type: 'reasoning', text: '内部草稿' },
      { type: 'text', text: '第一段' },
      { type: 'tool-call', text: '不该出现' },
      { type: 'text', text: '第二段' },
    ]);

    expect(text).toBe('第一段\n\n第二段');
    expect(text).not.toContain('内部草稿');
    expect(text).not.toContain('不该出现');
  });

  it('空分片、缺字段与空数组都退化为空串', () => {
    expect(messageTextForCopy([])).toBe('');
    expect(messageTextForCopy(null)).toBe('');
    expect(messageTextForCopy(undefined)).toBe('');
    expect(messageTextForCopy([{ type: 'text', text: '' }])).toBe('');
    expect(messageTextForCopy([{ type: 'text' }])).toBe('');
  });

  it('去掉首尾空白：只靠空白的分片不该让复制按钮可用', () => {
    expect(messageTextForCopy([{ type: 'text', text: '  \n ' }])).toBe('');
    expect(messageTextForCopy([{ type: 'text', text: '  有内容  ' }])).toBe('有内容');
  });
});
