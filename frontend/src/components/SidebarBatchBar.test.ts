import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { SidebarBatchBar, type SidebarBatchBarProps } from './SidebarBatchBar';

const render = (overrides: Partial<SidebarBatchBarProps> = {}) =>
  renderToStaticMarkup(
    createElement(SidebarBatchBar, {
      selectedCount: 2,
      isAllSelected: false,
      canSelectAll: true,
      onToggleSelectAll: vi.fn(),
      onExitSelectMode: vi.fn(),
      onPinSelected: vi.fn(),
      onUnpinSelected: vi.fn(),
      onRequestBatchDelete: vi.fn(),
      onRequestClearAll: vi.fn(),
      canClearAll: true,
      ...overrides,
    }),
  );

describe('SidebarBatchBar', () => {
  it('整个批量栏是一个有名字的 group，选中数会向读屏播报', () => {
    const html = render();

    expect(html).toContain('role="group"');
    expect(html).toContain('aria-label="批量操作"');
    expect(html).toContain('aria-live="polite"');
    expect(html).toContain('已选 2 项');
  });

  it('没有可见会话时禁用「全选」，避免点了没反应', () => {
    const disabled = render({ canSelectAll: false });
    const enabled = render({ canSelectAll: true });

    expect(disabled).toContain('disabled=""');
    // 只断言全选按钮所在片段：其他按钮在空选择时也会 disabled
    expect(disabled).toContain('disabled="" class="hover:text-foreground');
    expect(enabled).not.toContain('disabled="" class="hover:text-foreground');
  });

  it('未选中任何会话时批量操作按钮禁用', () => {
    const html = render({ selectedCount: 0 });

    expect(html).toContain('已选 0 项');
    expect(html).toContain('disabled=""');
  });

  it('没传导出回调时不渲染导出入口；没有会话时不渲染清空入口', () => {
    const withoutExport = render({ onExportSelected: undefined, canClearAll: false });

    expect(withoutExport).not.toContain('导出选中');
    expect(withoutExport).not.toContain('清除所有会话');
  });

  it('全选态显示「取消全选」', () => {
    expect(render({ isAllSelected: true })).toContain('取消全选');
  });
});
