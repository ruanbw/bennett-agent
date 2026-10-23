/**
 * 思考等级选择器已被合并进 `ModelSelector`：这里只锁住「保留导出、但不渲染任何东西」。
 *
 * 为什么要留这个文件：`thread.aui.tsx` 的接线由 Lead 统一改，在此之前任何仍在引用
 * `ReasoningEffortSelector` 的地方都不该因为「找不到模块」而编译失败。
 */

import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { ReasoningEffortSelector } from './ReasoningEffortSelector';

describe('ReasoningEffortSelector', () => {
  it('保留导出，但不再渲染任何独立按钮（等级已收进 ModelSelector 的菜单）', () => {
    const html = renderToStaticMarkup(
      createElement(ReasoningEffortSelector, {
        levels: ['off', 'low', 'medium', 'high'],
        currentEffort: 'high',
        onSelectEffort: vi.fn(),
      }),
    );

    expect(html).toBe('');
  });
});
