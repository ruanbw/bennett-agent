import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

/**
 * `TraceGroup` 依赖 assistant-ui 的 `useAuiState`（读消息部件），node 环境里没有
 * 运行时上下文，所以这里把 `@assistant-ui/react` 与 base-ui 的 Collapsible 换掉，
 * 只验证**组件层自己的契约**：
 *   - 「完全展开」档不渲染组头，过程行直接铺开；
 *   - 其余三档渲染一个无边框组头，且组头文案随档位变化；
 *   - 详细档的**历史**轮次默认展开。
 *
 * 文案本身在 `lib/trace-summary.test.ts` / `lib/step-process.test.ts` 里逐条覆盖，
 * 这里不重复断言词条，只钉「档位 → 露什么」。
 */
vi.mock('@assistant-ui/react', () => ({
  useAuiState: (selector: (state: unknown) => unknown) =>
    selector({ message: { parts: [] } }),
}));

/** Collapsible 系列换成能透出 open 状态的朴素元素（base-ui 要 document）。 */
vi.mock('@/components/ui/collapsible', () => ({
  Collapsible: ({ open, children }: { open?: boolean; children?: React.ReactNode }) =>
    createElement('div', { 'data-open': open ? 'true' : 'false' }, children),
  CollapsibleTrigger: ({
    children,
    render,
  }: {
    children?: React.ReactNode;
    render?: React.ReactElement<Record<string, unknown>>;
  }) => {
    const props = (render?.props ?? {}) as Record<string, unknown>;
    return createElement('button', { type: 'button', ...props }, children);
  },
  CollapsibleContent: ({ children }: { children?: React.ReactNode }) =>
    createElement('div', { 'data-panel': '' }, children),
}));

import { TraceGroup } from './trace-group';
import { DetailLevelContext } from '@/context/detail-level-context';
import {
  DEFAULT_COLLAPSE_TIMING,
  presentationPolicyFor,
  type DetailLevel,
} from '@/lib/detail-level';

/**
 * `TraceGroup` 的 `children` 是必填项，而 `createElement` 的第三参在 React 19 的
 * 类型里不再自动补成 `children` 属性——所以单独构造 props 对象，而不是写成
 * `createElement(Comp, props, child)`。
 */
function traceGroupProps(settled: boolean) {
  return {
    indices: [] as readonly number[],
    counts: { running: 0, complete: 1, incomplete: 0, requiresAction: 0 },
    settled,
    children: createElement('span', { 'data-child': '' }, '过程行'),
  };
}

function render(level: DetailLevel, settled = true) {
  return renderToStaticMarkup(
    createElement(
      DetailLevelContext.Provider,
      {
        value: {
          level,
          setLevel: () => {},
          collapseTiming: DEFAULT_COLLAPSE_TIMING,
          setCollapseTiming: () => {},
          policy: presentationPolicyFor(level, DEFAULT_COLLAPSE_TIMING),
        },
      },
      createElement(TraceGroup, traceGroupProps(settled)),
    ),
  );
}

describe('过程组的档位契约', () => {
  it('完全展开档不渲染组头，过程行直接铺开', () => {
    const html = render('verbose');

    expect(html).toContain('data-trace-mode="inline"');
    expect(html).toContain('过程行');
    // 没有可点的组头 —— 这一档的语义就是「不再折叠成组」
    expect(html).not.toContain('<button');
  });

  it('其余三档渲染一个无边框组头（不带边框类）', () => {
    for (const level of ['compact', 'standard', 'detailed'] as const) {
      const html = render(level);
      expect(html).toContain('<button');
      expect(html).not.toContain('data-trace-mode="inline"');
      // 组头是一条文字行：加边框会让它看起来像一张独立卡片，和正文抢视觉重量
      expect(html).not.toMatch(/<button[^>]*border(?![-\w]*border-border\/50)/);
    }
  });

  it('详细档的历史轮次默认展开', () => {
    const settled = render('detailed', true);
    expect(settled).toContain('data-open="true"');

    // 进行中的轮次仍收起：流式增量插入会让高度反复跳动
    const live = render('detailed', false);
    expect(live).toContain('data-open="false"');
  });

  it('标准/简洁档默认收起（历史轮次也不展开）', () => {
    for (const level of ['compact', 'standard'] as const) {
      expect(render(level)).toContain('data-open="false"');
    }
  });

  it('组头是无边框的一行文字，带 aria-label 说明展开后看到什么', () => {
    const html = render('standard');
    expect(html).toContain('aria-label=');
    expect(html).toContain('展开过程');
  });
});
