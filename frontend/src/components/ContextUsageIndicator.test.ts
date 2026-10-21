import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { CONTEXT_CRITICAL_PERCENT } from '@/lib/context-usage';
import type { UsageState } from '@/types';

import { ContextUsageIndicator } from './ContextUsageIndicator';

/**
 * 用量指示器的渲染契约（SSR，仓库没有 jsdom）。
 *
 * 三条规则最容易在改样式时被顺手改坏，也都是「看起来对、实际误导」那一类：
 * 锁定时不能展示旧数字、没用量的会话不该出现图标、接近上限必须有可见提示。
 */
const render = (props: Partial<Parameters<typeof ContextUsageIndicator>[0]> = {}) =>
  renderToStaticMarkup(createElement(ContextUsageIndicator, { usage: null, ...props }));

const usage = (overrides: Partial<UsageState> = {}): UsageState => ({
  contextTokens: 851,
  contextWindow: 1_000_000,
  contextLimitTokens: null,
  outputTokens: 2,
  cacheReadTokens: 0,
  ...overrides,
});

describe('ContextUsageIndicator', () => {
  it('没有用量且未锁定时整个指示器不出现', () => {
    expect(render()).toBe('');
  });

  it('有用量时显示「已用 / 上限」并在 aria-label 里带上完整占比', () => {
    const html = render({ usage: usage(), currentModel: 'auto/x', usageModel: 'auto/x' });

    expect(html).toContain('851 / 1.0M');
    expect(html).toContain('上下文用量：上下文 851 / 1.0M');
    expect(html).toContain('aria-haspopup="dialog"');
  });

  it('锁定时只说明「已满」，不展示上一轮那个可能还很宽裕的数字', () => {
    const html = render({
      usage: usage({ contextTokens: 9_000, contextWindow: 18_000, contextLimitTokens: 18_000 }),
      locked: true,
      currentModel: 'auto/x',
      usageModel: 'auto/x',
    });

    expect(html).toContain('已满');
    expect(html).toContain('上下文已满，查看说明');
    // 旧数字一个字都不该出现在收起态
    expect(html).not.toContain('9,000');
  });

  it(`接近上限（≥${CONTEXT_CRITICAL_PERCENT}%）时收起态就变成警示色`, () => {
    const html = render({
      usage: usage({ contextTokens: 17_500, contextWindow: 18_000, contextLimitTokens: 18_000 }),
      currentModel: 'auto/x',
      usageModel: 'auto/x',
    });

    expect(html).toContain('text-destructive');
  });

  it('用量很小的时候不是警示色（否则天天红着，警示就失效了）', () => {
    const html = render({ usage: usage(), currentModel: 'auto/x', usageModel: 'auto/x' });

    expect(html).not.toContain('text-destructive');
  });

  it('窗口未知时只报绝对值，不编造百分比', () => {
    const html = render({
      usage: usage({ contextWindow: null }),
      currentModel: 'auto/new',
      usageModel: 'auto/old',
      modelContextWindow: null,
    });

    expect(html).toContain('851');
    expect(html).toContain('（窗口未知）');
  });
});

describe('ContextUsageIndicator 的档位提示', () => {
  /**
   * 需要一个能把弹层渲染出来的开关：这是一个「点开才显示」的弹层，SSR 默认是收起的。
   * `initialOpen` 只用于测试与将来的「默认展开」需求。
   */
  const renderPopover = (props: Partial<Parameters<typeof ContextUsageIndicator>[0]> = {}) =>
    renderToStaticMarkup(
      createElement(ContextUsageIndicator, { usage: null, initialOpen: true, ...props }),
    );

  it('七成到九成之间给出「还能继续」的琥珀提示（红条只在九成以上）', () => {
    const html = renderPopover({
      usage: usage({ contextTokens: 14_000, contextWindow: 18_000, contextLimitTokens: 18_000 }),
      currentModel: 'auto/x',
      usageModel: 'auto/x',
    });

    expect(html).toContain('已用过七成');
    expect(html).not.toContain('已接近可用上限');
  });

  it('九成以上换成红色的「下一条可能被拒绝」', () => {
    const html = renderPopover({
      usage: usage({ contextTokens: 17_500, contextWindow: 18_000, contextLimitTokens: 18_000 }),
      currentModel: 'auto/x',
      usageModel: 'auto/x',
    });

    expect(html).toContain('已接近可用上限');
    expect(html).not.toContain('已用过七成');
  });

  it('用量很小时两条提示都不出现（天天提示等于没提示）', () => {
    const html = renderPopover({ usage: usage(), currentModel: 'auto/x', usageModel: 'auto/x' });

    expect(html).not.toContain('已用过七成');
    expect(html).not.toContain('已接近可用上限');
  });
});

describe('剩余空间的「人话」参照', () => {
  it('展开后说明还能写多少字（token 对用户没有直觉）', () => {
    const html = render({ usage: usage({ contextTokens: 12_000, contextLimitTokens: 18_000 }), initialOpen: true });

    expect(html).toContain('还能写约 6,000 字');
    // 必须写明是粗估与英文口径，别把它当成精确换算
    expect(html).toContain('粗估');
    expect(html).toContain('英文约 4 字符/token');
  });

  it('没有余量（或上限未知）时不渲染那一行', () => {
    const exhausted = render({
      usage: usage({ contextTokens: 20_000, contextLimitTokens: 18_000 }),
      initialOpen: true,
    });
    expect(exhausted).not.toContain('还能写约');

    const unknown = render({ usage: usage({ contextLimitTokens: null, contextWindow: null }), initialOpen: true });
    expect(unknown).not.toContain('还能写约');
  });
});
