/**
 * 「本机数据」区块的三种状态锁。
 *
 * 这个应用的数据默认只存在本机浏览器里，所以这块的失败态必须说清楚是「读不出来」而不是
 * 「数据没了」——一行 dash 会被读成后者。
 */

import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { LocalDataStatsSection } from './LocalDataStatsSection';

const render = (props: {
  stats: Parameters<typeof LocalDataStatsSection>[0]['stats'];
  failed: boolean;
  onRetry?: () => void;
}) => renderToStaticMarkup(createElement(LocalDataStatsSection, props));

describe('LocalDataStatsSection', () => {
  it('读到数据时展示会话数、消息数与存储占用', () => {
    const html = render({
      stats: { sessions: 3, messages: 42, usageBytes: 3 * 1024 * 1024, quotaBytes: 10 * 1024 * 1024 * 1024 },
      failed: false,
    });

    expect(html).toContain('3 个');
    expect(html).toContain('42 条');
    expect(html).toContain('3.0 MB');
    expect(html).not.toContain('role="alert"');
  });

  it('还没读完时是占位符，不冒充「0 个会话」', () => {
    const html = render({ stats: null, failed: false });

    expect(html).toContain('—');
    expect(html).not.toContain('0 个');
  });

  it('读失败时给出原因、重试入口，并说明不代表数据丢失', () => {
    const onRetry = vi.fn();
    const html = render({ stats: null, failed: true, onRetry });

    expect(html).toContain('role="alert"');
    expect(html).toContain('不代表会话内容已经丢失');
    expect(html).toContain('重新读取');
  });

  it('没有重试回调时只说明原因，不渲染空按钮', () => {
    const html = render({ stats: null, failed: true });

    expect(html).toContain('role="alert"');
    expect(html).not.toContain('重新读取');
  });
});
