import { describe, expect, it } from 'vitest';

import { logoutWarning } from './logout-warning';

describe('logoutWarning', () => {
  it('数量未知时明确说正在检查，而不是先给一句「没有未同步」', () => {
    expect(logoutWarning(null, false)).toContain('正在检查');
  });

  it('有未同步内容时报出具体条数并说明不可恢复', () => {
    const text = logoutWarning(3, false);
    expect(text).toContain('3 条');
    expect(text).toContain('无法恢复');
  });

  it('没有未同步内容时说明会清空本机会话记录', () => {
    expect(logoutWarning(0, false)).toContain('清空本机保存的会话记录');
  });

  it('读取失败不伪装成 0 条，而是如实提示无法确认', () => {
    const text = logoutWarning(0, true);
    expect(text).toContain('无法读取');
    expect(text).not.toContain('清空本机保存的会话记录');
  });
});
