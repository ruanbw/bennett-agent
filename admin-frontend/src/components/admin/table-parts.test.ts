/**
 * 列表页共享骨架的静态结构锁。
 *
 * 纯计算那半（`pageWindow`）已由 `lib/admin.test.ts` 覆盖；这里用 react-dom/server
 * 锁「闭着的时候用户看到什么、读屏听到什么」：Badge 的危险色、PageShell 的标题层级
 * 与 aria 关联、状态行的 role=status、错误提示条的 role=alert。这些结构是三个页面
 * （概览/提供商/用户）共用的可访问性契约，坏了是三页一起坏。
 *
 * children 统一写进 props 对象而非 createElement 第三参：后者在 TS 的重载下会丢
 * children 的类型关联（typecheck 过不去）。
 */

import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { Badge, InlineAlert, PageShell, TableStatusRow } from './table-parts';

describe('Badge', () => {
  it('渲染为徽标 span，文本可见', () => {
    const html = renderToStaticMarkup(createElement(Badge, { tone: 'ok', children: '已启用' }));

    expect(html).toContain('<span');
    expect(html).toContain('rounded-full');
    expect(html).toContain('已启用');
  });

  it('危险 tone 带上 destructive 类，封禁状态不被渲染成普通灰徽标', () => {
    const html = renderToStaticMarkup(createElement(Badge, { tone: 'danger', children: '已封禁' }));

    expect(html).toContain('text-destructive');
    expect(html).toContain('已封禁');
  });
});

describe('PageShell', () => {
  function renderPageShell(actions?: string) {
    return renderToStaticMarkup(
      createElement(PageShell, {
        title: '模型提供商',
        description: '维护上游提供商与模型清单。',
        children: createElement('p', null, '面板内容'),
        ...(actions ? { actions } : {}),
      }),
    );
  }

  it('标题进 h2 且通过 aria-labelledby 关联到 section（读屏报得出页面名）', () => {
    const html = renderPageShell();

    expect(html).toContain('<h2 id="page-heading"');
    expect(html).toContain('模型提供商');
    expect(html).toContain('aria-labelledby="page-heading"');
  });

  it('说明文字与面板内容都渲染', () => {
    const html = renderPageShell();

    expect(html).toContain('维护上游提供商与模型清单。');
    expect(html).toContain('面板内容');
  });

  it('有 actions 时渲染在页头；没有时不渲染空的 actions 容器', () => {
    expect(renderPageShell('新增提供商')).toContain('新增提供商');
    expect(renderPageShell()).not.toContain('shrink-0');
  });
});

describe('TableStatusRow', () => {
  function renderStatus(loading?: boolean) {
    return renderToStaticMarkup(
      createElement('table', null, createElement('tbody', null, createElement(TableStatusRow, {
        colSpan: 3,
        text: loading ? '加载中…' : '还没有提供商',
        ...(loading ? { loading } : {}),
      }))),
    );
  }

  it('占满给定列数（colSpan 透传，列少一列就错位）', () => {
    // React 19 的 static markup 保留 camelCase 属性名，按大小写不敏感匹配
    expect(renderStatus()).toMatch(/colspan="3"/i);
  });

  it('加载态的文案带 role=status（读屏主动播报，不靠用户扫到）', () => {
    const html = renderStatus(true);

    expect(html).toContain('加载中…');
    expect(html).toContain('role="status"');
    // 加载转圈图标对读屏是噪音
    expect(html).toContain('aria-hidden="true"');
  });

  it('空态渲染说明文案且不出现 status 角色', () => {
    const html = renderStatus();

    expect(html).toContain('还没有提供商');
    expect(html).not.toContain('role="status"');
  });
});

describe('InlineAlert', () => {
  it('默认 error tone：role=alert，失败信息主动打断读屏', () => {
    const html = renderToStaticMarkup(
      createElement(InlineAlert, { children: '加载失败' }),
    );

    expect(html).toContain('role="alert"');
    expect(html).toContain('text-destructive');
    expect(html).toContain('加载失败');
  });

  it('info tone 是 status 而非 alert：提示条不该打断用户当前操作', () => {
    const html = renderToStaticMarkup(
      createElement(InlineAlert, { tone: 'info', children: '已恢复到最新数据' }),
    );

    expect(html).toContain('role="status"');
    expect(html).not.toContain('role="alert"');
  });
});
