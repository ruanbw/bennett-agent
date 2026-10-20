import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { SidebarSearchEmpty } from './SidebarSearchEmpty';

const render = (tokens: string[]) =>
  renderToStaticMarkup(
    createElement(SidebarSearchEmpty, { tokens, onClearSearch: () => {} }),
  );

interface ElementLike {
  props?: Record<string, unknown> & { children?: unknown };
}

/** 在 React 元素树里找第一个带指定 prop 的元素（组件未渲染成 DOM 前也能验证接线）。 */
function findElementByProp(node: unknown, prop: string): ElementLike | null {
  if (!node || typeof node !== 'object') return null;
  if (Array.isArray(node)) {
    for (const child of node) {
      const hit = findElementByProp(child, prop);
      if (hit) return hit;
    }
    return null;
  }
  const element = node as ElementLike;
  if (element.props?.[prop]) return element;
  return findElementByProp(element.props?.children, prop);
}

describe('SidebarSearchEmpty', () => {
  it('回显当前词表，并说明多个关键词需要同时命中', () => {
    const html = render(['财报', 'Q3']);

    expect(html).toContain('「财报」');
    expect(html).toContain('「Q3」');
    expect(html).toContain('同时包含');
    expect(html).toContain('多个关键词需要同时命中');
  });

  it('说明搜索覆盖标题、正文与工具调用', () => {
    const html = render(['部署']);

    expect(html).toContain('标题');
    expect(html).toContain('消息正文');
    expect(html).toContain('工具调用');
  });

  it('提供清除搜索的入口', () => {
    const html = render(['部署']);

    expect(html).toContain('清除搜索');
    expect(html).toContain('<button');
  });

  it('词表里的尖括号不会被当成标记（按文本转义）', () => {
    const html = render(['<script>alert(1)</script>']);

    expect(html).not.toContain('<script>');
    expect(html).toContain('&lt;script&gt;');
  });

  it('空态整体对读屏是一个状态区域', () => {
    const html = render(['x']);

    expect(html).toContain('role="status"');
  });

  it('没有词表时仍然渲染（调用方只在有词时挂载，这里保证兜底不崩）', () => {
    const html = render([]);

    expect(html).toContain('清除搜索');
  });

  it('清除按钮把 onClearSearch 原样接到 onClick 上', () => {
    const onClearSearch = vi.fn();
    // SSR 不执行交互，直接遍历元素树找到带 onClick 的节点（组件是纯函数，可当数据看）
    const tree = SidebarSearchEmpty({ tokens: ['a'], onClearSearch });
    const button = findElementByProp(tree, 'onClick');

    expect(button).not.toBeNull();
    (button!.props as { onClick: () => void }).onClick();
    expect(onClearSearch).toHaveBeenCalledTimes(1);
  });
});
