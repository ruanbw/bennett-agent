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
