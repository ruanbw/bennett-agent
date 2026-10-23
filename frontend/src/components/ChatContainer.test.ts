import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { ChatContainer } from './ChatContainer';

vi.mock('./assistant-ui/elements/thread.aui', () => ({
  Thread: () => createElement('div', { 'data-testid': 'mock-thread' }, 'mock thread'),
}));

vi.mock('./ChatProvider', () => ({
  ChatSessionNotices: () => createElement('div', { 'data-testid': 'mock-notices' }),
}));

describe('ChatContainer 顶栏与可编辑标题', () => {
  it('展示传入的会话标题与可编辑提示', () => {
    const html = renderToStaticMarkup(
      createElement(ChatContainer, {
        models: [],
        currentModel: null,
        sessionTitle: '深度架构演进讨论',
        isSidebarOpen: true,
        onSelectModel: vi.fn(),
        onToggleSidebar: vi.fn(),
      })
    );

    expect(html).toContain('深度架构演进讨论');
    expect(html).toContain('title="点击修改会话标题"');
    // 侧边栏展开时不出现胶囊工具栏的「展开侧边栏」按钮
    expect(html).not.toContain('aria-label="展开侧边栏"');
  });

  it('侧边栏折叠时胶囊工具栏只有「展开 + 新建」', () => {
    const html = renderToStaticMarkup(
      createElement(ChatContainer, {
        models: [],
        currentModel: null,
        sessionTitle: '某个对话',
        isSidebarOpen: false,
        onSelectModel: vi.fn(),
        onToggleSidebar: vi.fn(),
        onCreateSession: vi.fn(),
      })
    );

    expect(html).toContain('aria-label="展开侧边栏"');
    expect(html).toContain('aria-label="新建对话"');
    expect(html).toContain('rounded-xl');
    // 搜索 / 设置 / 同步 / 账号都不进顶栏（移动端与桌面端一致）：
    // 它们在侧边栏与抽屉里各有常驻入口，挤进胶囊只会把会话标题压没。
    expect(html).not.toContain('aria-label="搜索对话"');
    expect(html).not.toContain('aria-label="设置"');
    expect(html).not.toContain('aria-label="已同步，查看同步详情"');
    expect(html).not.toContain('aria-label="账号与设置菜单"');
    // 移动端与桌面端看到的是同一组按钮，不再有「窄屏隐藏」的那层容器
    expect(html).not.toContain('md:flex');
  });

  it('未提供标题时优雅回退为「新对话」', () => {
    const html = renderToStaticMarkup(
      createElement(ChatContainer, {
        models: [],
        currentModel: null,
        isSidebarOpen: true,
        onSelectModel: vi.fn(),
        onToggleSidebar: vi.fn(),
      })
    );

    expect(html).toContain('新对话');
  });
});
