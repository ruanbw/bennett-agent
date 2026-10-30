import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { TurnAnnouncement, TurnAnnouncementText, TurnStatusRow } from './turn-status-row';

/**
 * 状态行是纯展示组件（无运行时上下文），用 react-dom/server 出静态 HTML 断言。
 * 文案逻辑在 `lib/step-process.turnStatusLabel` 里单测过，这里钉的是
 * 「什么时候渲染、渲染成什么语义」。
 */
const render = (props: Partial<Parameters<typeof TurnStatusRow>[0]> = {}) =>
  renderToStaticMarkup(createElement(TurnStatusRow, { reason: 'error', ...props }));

describe('异常结束的状态行', () => {
  it('正常结束不渲染任何东西——「已完成」不再挂在每条回答上', () => {
    expect(render({ reason: 'ok', elapsedMs: 204_000 })).toBe('');
  });

  it('被停止：文案 + 耗时，且带自己的结束原因', () => {
    const html = render({ reason: 'aborted', elapsedMs: 204_000 });
    expect(html).toContain('已停止');
    expect(html).toContain('用时 3分24秒');
    expect(html).toContain('data-turn-status="aborted"');
    expect(html).not.toContain('已完成');
  });

  it('中断与失败各有各的文案，不再统一成「已停止」', () => {
    expect(render({ reason: 'interrupted' })).toContain('回复不完整（已中断）');
    expect(render({ reason: 'error' })).toContain('处理失败');
  });

  it('拿不到耗时时只报状态，不编一个 0 秒', () => {
    const html = render({ reason: 'error', elapsedMs: null });
    expect(html).toContain('处理失败');
    expect(html).not.toContain('用时');
    expect(html).not.toContain('0秒');
  });

  it('是纯文本状态而不是控件：整轮折叠没实现，就不能长得像按钮', () => {
    const html = render({ reason: 'aborted', elapsedMs: 1_000 });
    expect(html.startsWith('<p')).toBe(true);
    expect(html).not.toContain('<button');
    expect(html).not.toContain('aria-expanded');
    expect(html).toContain('role="status"');
  });
});

/**
 * 流式结束后的播报。
 *
 * 组件靠一次渲染间的状态比对捕捉 `isRunning` 的 true→false，而 react-dom/server 只
 * 渲染一帧，所以这里钉两件事：静态结构（视觉隐藏、独立的 polite 状态区、初始不喧哗）
 * 与文案（只说轮次，不含正文）。
 */
describe('流式结束的播报', () => {
  const renderAnnouncement = (props: Partial<Parameters<typeof TurnAnnouncement>[0]> = {}) =>
    renderToStaticMarkup(createElement(TurnAnnouncement, { isRunning: false, ...props }));

  const renderText = (turnNumber: number | null) =>
    renderToStaticMarkup(createElement(TurnAnnouncementText, { turnNumber }));

  it('是视觉隐藏的独立 status 区，不靠 aria-live=assertive 也不带正文', () => {
    const html = renderAnnouncement();

    expect(html.startsWith('<p')).toBe(true);
    expect(html).toContain('role="status"');
    expect(html).toContain('sr-only');
    expect(html).not.toContain('aria-live="assertive"');
    // 初始为空：打开历史会话（本来就没在生成）不该凭空念一句「回复完成」
    expect(html).toBe('<p role="status" class="sr-only"></p>');
  });

  it('生成中不播报任何东西', () => {
    expect(renderAnnouncement({ isRunning: true, turnNumber: 3 })).toBe(
      '<p role="status" class="sr-only"></p>'
    );
  });

  it('文案只报轮次：正文不能进 live region（否则等于再念一遍整段）', () => {
    expect(renderText(3)).toBe('第 3 轮回复完成');
    expect(renderText(1)).toBe('第 1 轮回复完成');
  });

  it('拿不到轮次时退回一句短的，不编一个序号', () => {
    expect(renderText(null)).toBe('回复完成');
  });
});
