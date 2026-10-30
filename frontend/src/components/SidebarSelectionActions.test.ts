import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { SidebarSelectionActions, type SidebarSelectionActionsProps } from './SidebarSelectionActions';

const render = (overrides: Partial<SidebarSelectionActionsProps> = {}) =>
  renderToStaticMarkup(
    createElement(SidebarSelectionActions, {
      selectedCount: 2,
      allPinned: false,
      onTogglePin: vi.fn(),
      onRequestDelete: vi.fn(),
      ...overrides,
    }),
  );

describe('SidebarSelectionActions', () => {
  it('底部操作条是一个带选中数的 group', () => {
    const html = render();

    expect(html).toContain('role="group"');
    expect(html).toContain('aria-label="批量操作，已选 2 项"');
    expect(html).toContain('删除');
  });

  it('置顶按钮按选中集合切换文案，而不是并排两个互斥按钮', () => {
    expect(render({ allPinned: false })).toContain('>置顶<');
    expect(render({ allPinned: false })).not.toContain('取消置顶');
    expect(render({ allPinned: true })).toContain('>取消置顶<');
  });

  it('未选中任何会话时所有动作禁用', () => {
    const html = render({ selectedCount: 0 });
    const disabled = html.match(/disabled=""/g) ?? [];

    // 置顶 + 删除（未传导出回调）
    expect(disabled).toHaveLength(2);
  });

  it('没传导出回调时不渲染导出入口', () => {
    expect(render()).not.toContain('>导出<');
    expect(render({ onExportSelected: vi.fn() })).toContain('>导出<');
  });
});
