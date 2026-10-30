import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { SidebarSelectToolbar, type SidebarSelectToolbarProps } from './SidebarSelectToolbar';

const render = (overrides: Partial<SidebarSelectToolbarProps> = {}) =>
  renderToStaticMarkup(
    createElement(SidebarSelectToolbar, {
      selectedCount: 2,
      isAllSelected: false,
      canSelectAll: true,
      canClearAll: true,
      onToggleSelectAll: vi.fn(),
      onExitSelectMode: vi.fn(),
      onRequestClearAll: vi.fn(),
      ...overrides,
    }),
  );

describe('SidebarSelectToolbar', () => {
  it('整条工具条是一个有名字的 toolbar，选中数会向读屏播报', () => {
    const html = render();

    expect(html).toContain('role="toolbar"');
    expect(html).toContain('aria-label="多选操作"');
    expect(html).toContain('aria-live="polite"');
    expect(html).toContain('已选 2 项');
  });

  it('一个都没选时给出下一步提示，而不是「已选 0 项」', () => {
    expect(render({ selectedCount: 0 })).toContain('选择要管理的对话');
  });

  it('全选态显示「取消全选」', () => {
    expect(render({ isAllSelected: true })).toContain('取消全选');
    expect(render({ isAllSelected: false })).toContain('>全选<');
  });

  it('没有可见会话时禁用「全选」，避免点了没反应', () => {
    expect(render({ canSelectAll: false })).toMatch(/disabled=""[^>]*>全选<\/button>/);
    expect(render({ canSelectAll: true })).not.toMatch(/disabled=""[^>]*>全选<\/button>/);
  });

  it('没有会话时不渲染「清除所有会话」入口', () => {
    expect(render({ canClearAll: true })).toContain('aria-label="更多批量操作"');
    expect(render({ canClearAll: false })).not.toContain('更多批量操作');
  });
});
