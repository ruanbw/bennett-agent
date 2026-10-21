import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { ErrorBoundary } from './ErrorBoundary';

/**
 * 崩溃页的渲染契约。
 *
 * 用 `renderToStaticMarkup` 直接渲染类组件的 `render()` 结果（仓库没有 jsdom）：
 * 构造实例、把 state 摆成「已崩溃」再渲染，就能验证用户实际看到的那一屏。
 */
function renderCrashed(error: Error, fallbackTitle = '对话区域加载异常') {
  const boundary = new ErrorBoundary({ children: null, fallbackTitle });
  boundary.state = { hasError: true, error, showDetail: false };
  return renderToStaticMarkup(boundary.render() as React.ReactElement);
}

describe('ErrorBoundary 崩溃页', () => {
  it('显示中文结论而不是原始英文报错', () => {
    const html = renderCrashed(new TypeError("Cannot read properties of undefined (reading 'content')"));

    expect(html).toContain('数据与预期不一致');
    // 原始报错默认不直接铺在页面上（折叠区里才有）
    expect(html).not.toContain('Cannot read properties');
  });

  it('给出「重试」与「重新加载」两个动作', () => {
    const html = renderCrashed(new Error('boom'));

    expect(html).toContain('重试');
    expect(html).toContain('重新加载');
  });

  it('保留标题，让用户知道是哪一块坏了', () => {
    expect(renderCrashed(new Error('boom'))).toContain('对话区域加载异常');
    expect(renderCrashed(new Error('boom'), '管理后台加载异常')).toContain('管理后台加载异常');
  });

  it('详情默认收起：折叠区未展开时页面里没有技术原文', () => {
    const html = renderCrashed(new Error('something odd happened'));

    expect(html).toContain('查看错误详情');
    expect(html).not.toContain('something odd happened');
  });

  it('折叠区展开后显示技术原文（供反馈时复制）', () => {
    const boundary = new ErrorBoundary({ children: null });
    boundary.state = { hasError: true, error: new Error('something odd happened'), showDetail: true };

    const html = renderToStaticMarkup(boundary.render() as React.ReactElement);

    expect(html).toContain('收起错误详情');
    expect(html).toContain('something odd happened');
  });

  it('没有技术原文时不渲染折叠入口（不出现空按钮）', () => {
    const boundary = new ErrorBoundary({ children: null });
    boundary.state = { hasError: true, error: new Error(''), showDetail: false };

    const html = renderToStaticMarkup(boundary.render() as React.ReactElement);

    expect(html).not.toContain('查看错误详情');
  });

  it('未崩溃时原样渲染子树', () => {
    const boundary = new ErrorBoundary({ children: createElement('span', null, '正常内容') });

    expect(renderToStaticMarkup(boundary.render() as React.ReactElement)).toContain('正常内容');
  });
});
