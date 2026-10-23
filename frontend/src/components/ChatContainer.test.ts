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

// 同步入口依赖引擎与 Dexie（node 环境没有 indexedDB），这里只验证「胶囊工具栏是否把它放了出来」。
vi.mock('./SyncStatusButton', () => ({
  SyncStatusButton: () => createElement('button', { 'aria-label': '已同步，查看同步详情' }),
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

  it('侧边栏折叠时展示悬浮胶囊工具栏', () => {
    const html = renderToStaticMarkup(
      createElement(ChatContainer, {
        models: [],
        currentModel: null,
        sessionTitle: '某个对话',
        isSidebarOpen: false,
        onSelectModel: vi.fn(),
        onToggleSidebar: vi.fn(),
        onCreateSession: vi.fn(),
        onOpenSearch: vi.fn(),
        onOpenSettings: vi.fn(),
      })
    );

    expect(html).toContain('aria-label="展开侧边栏"');
    expect(html).toContain('aria-label="新建对话"');
    // 搜索框、设置页和同步面板都长在侧边栏里：收起后必须由胶囊工具栏补上入口
    expect(html).toContain('aria-label="搜索对话"');
    expect(html).toContain('aria-label="设置"');
    expect(html).toContain('aria-label="已同步，查看同步详情"');
    expect(html).toContain('rounded-xl');
    // 搜索/设置/同步/账号在移动端由抽屉承担，顶栏只留「展开 + 新建」：
    // 这四个包在一个 md 以下隐藏的容器里
    expect(html).toContain('hidden items-center md:flex');
    expect(html.indexOf('hidden items-center md:flex')).toBeLessThan(
      html.indexOf('aria-label="搜索对话"')
    );
    // 「展开侧边栏」「新建对话」不在这层容器里，移动端仍然可见
    expect(html.indexOf('aria-label="新建对话"')).toBeLessThan(
      html.indexOf('hidden items-center md:flex')
    );
  });

  it('没传 onOpenSearch 时不渲染搜索入口（不给出点不动的按钮）', () => {
    const html = renderToStaticMarkup(
      createElement(ChatContainer, {
        models: [],
        currentModel: null,
        isSidebarOpen: false,
        onSelectModel: vi.fn(),
        onToggleSidebar: vi.fn(),
      })
    );

    expect(html).toContain('aria-label="展开侧边栏"');
    expect(html).not.toContain('aria-label="搜索对话"');
    expect(html).not.toContain('aria-label="设置"');
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
