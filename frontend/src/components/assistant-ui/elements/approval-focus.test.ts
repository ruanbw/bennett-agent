/**
 * 审批卡片「出现时能被感知」的契约。
 *
 * 交互（逐条表态、二次确认、提交锁）由 approval-card.test.ts 与 Playwright 覆盖；
 * 这里专门锁 S5：卡片是**静默插进**正在打字的页面里的，如果它不播报、也不接管焦点，
 * 键盘用户继续按 Enter 就会发出一条新的普通提问，而待审批的这一轮被搁在一边。
 *
 * 焦点那部分抽成纯函数（`approval-focus.ts`）正是为了能在这里验：vitest 跑在 node
 * 环境，没有 jsdom，真实 focus 只能靠浏览器侧。
 */

import { describe, expect, it } from 'vitest';

import {
  approvalAnnouncement,
  approvalBatchSignature,
  approvalFocusTarget,
  shouldTakeApprovalFocus,
} from './approval-focus';

describe('approvalAnnouncement', () => {
  it('说清「几条 / 被拦下了 / 批准后才执行」，而不是只念「需要审批」', () => {
    const one = approvalAnnouncement(1);
    expect(one).toContain('1 条');
    expect(one).toContain('批准');
    expect(one).toContain('才会执行');
  });

  it('多条时说清要逐条表态', () => {
    const many = approvalAnnouncement(3);

    expect(many).toContain('3 条');
    expect(many).toContain('逐条');
  });

  it('0 条不播报（那种情况本来就不该渲染卡片）', () => {
    expect(approvalAnnouncement(0)).toBe('');
    expect(approvalAnnouncement(-1)).toBe('');
  });
});

describe('approvalBatchSignature', () => {
  it('只由 toolCallId 决定：同内容的新数组签名相同（挡住重渲染的重复抢焦点）', () => {
    const first = approvalBatchSignature([{ toolCallId: 'a' }, { toolCallId: 'b' }]);
    const rebuilt = approvalBatchSignature([{ toolCallId: 'a' }, { toolCallId: 'b' }]);

    expect(first).toBe(rebuilt);
  });

  it('换了一批调用签名就变（新一批该重新接管焦点）', () => {
    expect(approvalBatchSignature([{ toolCallId: 'a' }])).not.toBe(
      approvalBatchSignature([{ toolCallId: 'z' }]),
    );
  });

  it('空批次签名是空串，不会被误当成「有内容的一批」', () => {
    expect(approvalBatchSignature([])).toBe('');
  });
});

describe('shouldTakeApprovalFocus', () => {
  it('本次挂载还没接管过 → 该接管', () => {
    expect(shouldTakeApprovalFocus(null, 'a|b')).toBe(true);
  });

  it('同一批已经接管过 → 不再抢：否则用户按完「拒绝」就被抢回第一条', () => {
    expect(shouldTakeApprovalFocus('a|b', 'a|b')).toBe(false);
  });

  it('换了一批 → 重新接管', () => {
    expect(shouldTakeApprovalFocus('a|b', 'c')).toBe(true);
  });

  it('空批次永不接管', () => {
    expect(shouldTakeApprovalFocus(null, '')).toBe(false);
    expect(shouldTakeApprovalFocus('', '')).toBe(false);
  });
});

describe('approvalFocusTarget', () => {
  /** 这一组断言就是「为什么不直接聚焦批准按钮」的书面记录。 */
  it('落点是卡片容器，不是「批准」按钮', () => {
    expect(approvalFocusTarget(null, 'a')).toBe('container');
  });

  it('已经接管过就不再接管（交给用户自己移动焦点）', () => {
    expect(approvalFocusTarget('a', 'a')).toBe('none');
  });

  it('空批次不接管', () => {
    expect(approvalFocusTarget(null, '')).toBe('none');
  });

  it('三类落点互斥且只可能是 container / none', () => {
    const targets = [
      approvalFocusTarget(null, 'a'),
      approvalFocusTarget('a', 'a'),
      approvalFocusTarget(null, ''),
    ];

    expect(new Set(targets)).toEqual(new Set(['container', 'none']));
  });
});
