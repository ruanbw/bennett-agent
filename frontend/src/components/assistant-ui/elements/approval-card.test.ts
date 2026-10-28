/**
 * 审批卡片的静态结构锁。
 *
 * 交互（逐条表态、二次确认、提交时锁住）由 Playwright 覆盖；这里锁那些改样式或
 * 改分支时最容易丢、却直接决定「用户能不能做出知情决定」的东西：
 *
 * 1. **参数必须显示**：工具名不足以判断后果（`delete_document` 删的是哪一篇？）；
 * 2. **风险等级必须显示**：用户要能看出哪条是不可撤销的；
 * 3. **未表态时提交按钮不可用**：否则用户会以为「没选的等于没批准」，而实现若改成
 *    默认拒绝，模型就会收到一个用户没做出的决定。
 */

import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { ApprovalCard } from './approval-card';

const dangerousCall = {
  toolCallId: 'c1',
  toolName: 'delete_document',
  argsText: '{"document_id": 42, "confirm": true}',
  description: '删除一篇知识库文档（不可撤销）',
  riskLevel: 'dangerous',
};

const readOnlyCall = {
  toolCallId: 'c2',
  toolName: 'search_knowledge_base',
  argsText: '{"query": "退款"}',
  description: '在知识库里检索',
  riskLevel: 'read_only',
};

function render(toolCalls = [dangerousCall]) {
  return renderToStaticMarkup(
    createElement(ApprovalCard, { toolCalls, onSubmit: vi.fn(async () => true) }),
  );
}

describe('ApprovalCard', () => {
  it('显示工具名、说明与完整参数', () => {
    const html = render();

    expect(html).toContain('delete_document');
    expect(html).toContain('删除一篇知识库文档');
    // 参数是审批的核心信息：没有它，用户只能盲签
    expect(html).toContain('document_id');
    expect(html).toContain('42');
  });

  it('显示风险等级', () => {
    expect(render()).toContain('高风险');
    expect(render([readOnlyCall])).toContain('只读');
  });

  it('说清「尚未执行」', () => {
    expect(render()).toContain('尚未执行');
  });

  /**
   * S5：卡片静默出现 = 用户不知道有东西在等他表态。
   *
   * 这几条只锁静态结构——真实焦点在浏览器侧，纯函数那半在 `approval-focus.test.ts`。
   */
  it('挂一块即时播报（role=alert + sr-only），提醒读屏用户有调用待批', () => {
    const html = render([dangerousCall, readOnlyCall]);

    // role=alert 是隐式 assertive 实时区域：节点插入即播报
    expect(html).toContain('role="alert"');
    expect(html).toContain('sr-only');
    // 播报内容要说清条数，不能只说「需要审批」
    expect(html).toContain('2 条工具调用需要你批准');
  });

  it('播报文案挂在卡片的 aria-describedby 上，聚焦卡片时能听到', () => {
    const html = render();

    const describedBy = html.match(/aria-describedby="([^"]+)"/)?.[1];
    expect(describedBy, '卡片没有 aria-describedby').toBeTruthy();
    // 被引用的那个 id 必须真实存在于同一份 HTML 里，否则读屏读到的是空
    expect(html).toContain(`id="${describedBy}"`);
  });

  it('容器可被脚本聚焦但不在 Tab 序列里（焦点接管用它，Tab 不额外停一站）', () => {
    const html = render();
    const container = html.slice(0, html.indexOf('>') + 1);

    expect(container).toContain('tabindex="-1"');
    // 有 tabindex="-1" 就足够被 focus()；不要把它写成 0（那会让 Tab 多停一站）
    expect(container).not.toContain('tabindex="0"');
  });

  it('「批准」按钮保持普通按钮语义，不会因为焦点接管而变成默认动作', () => {
    const html = render();

    // 阻止「聚焦即批准」：不放 autofocus，也不给它 primary 那种默认动作的样式语义
    expect(html).not.toContain('autofocus');
  });

  it('未表态时「提交决定」按钮是禁用的', () => {
    const html = render();

    // 把断言绑在**那个**按钮上：先取出「提交决定」所在按钮的完整标签块，再要求它
    // 带 disabled。只断言页面里存在 disabled 是没有意义的——忽略按钮、禁用图标都可能
    // 让用例通过，而提交其实仍可点。
    const submitButton = html.match(/<button[^>]*>(?:(?!<\/button>).)*?提交决定(?:(?!<\/button>).)*?<\/button>/s)?.[0];
    expect(submitButton, '没找到提交按钮').toBeTruthy();
    expect(submitButton).toContain('disabled=""');
  });

  it('多条调用时显示待确认数量', () => {
    expect(render([dangerousCall, readOnlyCall])).toContain('2 条待确认');
  });

  it('参数为空对象时给出可读占位而不是一对空花括号', () => {
    const html = render([{ ...readOnlyCall, argsText: '{}' }]);

    expect(html).toContain('无参数');
  });

  it('参数不是合法 JSON 时原样显示，不吞掉', () => {
    const html = render([{ ...readOnlyCall, argsText: '{broken' }]);

    expect(html).toContain('broken');
  });

  it('提供忽略入口', () => {
    const html = renderToStaticMarkup(
      createElement(ApprovalCard, {
        toolCalls: [dangerousCall],
        onSubmit: vi.fn(async () => true),
        onDismiss: vi.fn(),
      }),
    );

    expect(html).toContain('暂不处理这些调用');
  });
});
