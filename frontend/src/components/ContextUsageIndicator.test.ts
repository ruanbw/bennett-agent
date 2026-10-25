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

describe('圆环视觉（与 DSH 的 ContextMeter 对齐）', () => {
  it('收起态是一个 14px 圆环 + 百分比，而不是横向进度条', () => {
    const html = render({ usage: usage(), currentModel: 'auto/x', usageModel: 'auto/x' });

    // 几何逐值对齐：viewBox 0 0 14 14、r=5.5、描边 2px、从 12 点方向顺时针
    expect(html).toContain('viewBox="0 0 14 14"');
    expect(html).toContain('r="5.5"');
    expect(html).toContain('stroke-width="2"');
    expect(html).toContain('transform="rotate(-90 7 7)"');
    // 851 / 1.0M = 0.085% → 显示 <1%，而不是四舍五入成 0%（看起来像没用量）
    expect(html).toContain('&lt;1%');
  });

  it('圆环填充比例就是占比（用 stroke-dasharray 表达，不是 CSS 宽度）', () => {
    const html = render({
      usage: usage({ contextTokens: 9_000, contextWindow: 18_000, contextLimitTokens: 18_000 }),
      currentModel: 'auto/x',
      usageModel: 'auto/x',
    });

    // 9000/18000 = 50% → dasharray 前半 = 周长的一半（2π*5.5/2 ≈ 17.28）
    expect(html).toContain('stroke-dasharray="17.27875959474386 34.55751918948772"');
    expect(html).toContain('50%');
  });

  it('锁定时圆环画满，读数写「已满」', () => {
    const html = render({ usage: usage(), locked: true });

    expect(html).toContain('已满');
    // 画满 = dasharray 前后相等（周长）
    expect(html).toContain('stroke-dasharray="34.55751918948772 34.55751918948772"');
  });

  it('比例很大但不到 1% 时写成 <1%（不写成 0%）', () => {
    const html = render({
      usage: usage({ contextTokens: 500, contextWindow: 1_000_000, contextLimitTokens: 1_000_000 }),
      currentModel: 'auto/x',
      usageModel: 'auto/x',
    });

    expect(html).toContain('&lt;1%');
  });
});

describe('上下文档的分段条', () => {
  it('拿不到分段时退化成单色整条，绝不编造三段比例', () => {
    const html = render({ usage: usage(), initialOpen: true, currentModel: 'auto/x', usageModel: 'auto/x' });

    expect(html).toContain('role="progressbar"');
    expect(html).toContain('aria-label="上下文占用"');
    // 图例只在真有分段时出现
    expect(html).not.toContain('工具定义');
    expect(html).not.toContain('对话消息');
    // 并且明确说明「没拆分」而不是让人以为条上只有一段是设计如此
    expect(html).toContain('没有回报上下文各成分的占比');
  });

  it('有分段时按比例切三段并给出图例与近似值', () => {
    const html = render({
      usage: usage({ contextTokens: 10_000, contextWindow: 100_000, contextLimitTokens: 100_000 }),
      initialOpen: true,
      currentModel: 'auto/x',
      usageModel: 'auto/x',
      breakdown: { systemTokens: 2_000, toolsTokens: 3_000, messageTokens: 5_000 },
    });

    expect(html).toContain('系统提示词');
    expect(html).toContain('工具定义');
    expect(html).toContain('对话消息');
    // 10% 已用，按 2:3:5 切成 2% / 3% / 5%
    expect(html).toContain('width:2%');
    expect(html).toContain('width:3%');
    expect(html).toContain('width:5%');
    expect(html).toContain('~2k');
  });

  it('分段数据全为 0 时同样退化成单色（不是三条 0 宽的线）', () => {
    const html = render({
      usage: usage(),
      initialOpen: true,
      currentModel: 'auto/x',
      usageModel: 'auto/x',
      breakdown: { systemTokens: 0, toolsTokens: 0, messageTokens: 0 },
    });

    expect(html).not.toContain('工具定义');
  });

  it('顶部标题是「上下文已用 + 百分比 + 右侧绝对值」（与 DSH 的 header 一致）', () => {
    const html = render({
      usage: usage({ contextTokens: 9_000, contextWindow: 18_000, contextLimitTokens: 18_000 }),
      initialOpen: true,
      currentModel: 'auto/x',
      usageModel: 'auto/x',
    });

    expect(html).toContain('上下文已用');
    expect(html).toContain('50%');
    expect(html).toContain('~9k / 18k');
  });
});
