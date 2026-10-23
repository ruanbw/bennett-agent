import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { ReasoningView } from './reasoning-view';
import { DetailLevelContext } from '@/context/detail-level-context';
import {
  DEFAULT_COLLAPSE_TIMING,
  presentationPolicyFor,
  type DetailLevel,
} from '@/lib/detail-level';

/**
 * 思考行只依赖「对话显示」上下文（不碰 assistant-ui 运行时），所以可以用
 * react-dom/server 直接渲染出静态 HTML 断言。
 *
 * 文案逻辑（预览取哪一行、去 `**`）在 `lib/trace-summary.ts` 里单测过；
 * 这里钉的是「档位 → 露什么」这条组件级契约。
 */
function render(text: string, level: DetailLevel, isStreaming = false, elapsedMs = 0) {
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
      createElement(ReasoningView, { text, isStreaming, elapsedMs }),
    ),
  );
}

describe('思考行的档位契约', () => {
  const text = '先确认现有实现\n第二行细节';

  it('始终只有「思考」两个字加图标，不再是「已思考（15s）」', () => {
    const html = render(text, 'standard');
    expect(html).toContain('思考');
    expect(html).not.toContain('已思考');
    expect(html).toContain('lucide-brain');
  });

  it('简洁档不显示预览：那一档的主张就是「过程别占地方」', () => {
    const html = render(text, 'compact');
    expect(html).toContain('思考');
    expect(html).not.toContain('先确认现有实现');
  });

  it('标准/详细/完全展开档显示一行预览', () => {
    for (const level of ['standard', 'detailed', 'verbose'] as const) {
      const html = render(text, level);
      expect(html).toContain('先确认现有实现');
      // 预览只取首行，不把第二行也渲染进来
      expect(html).not.toContain('第二行细节');
    }
  });

  it('运行中也显示预览：取最后一个已完成段落的首行', () => {
    const html = render('第一段\n\n正在写的半句', 'standard', true);
    expect(html).toContain('第一段');
    expect(html).not.toContain('正在写的半句');
  });

  it('没有文本也不流式时整个组件不渲染（不留空行）', () => {
    expect(render('', 'standard')).toBe('');
  });

  it('流式中但没有可用预览时，只显示「思考」不回显半句话', () => {
    const html = render('还没换行的一句', 'standard', true);
    expect(html).toContain('思考');
    expect(html).not.toContain('还没换行的一句');
  });

  it('耗时收起时也显示，并固定在行尾：位置不随预览长度左右跳', () => {
    const html = render(text, 'standard', false, 15_000);

    expect(html).toContain('15s');
    // ml-auto 把耗时推到行尾：预览再长也只是自己截断，不会把耗时挤到左边
    expect(html).toContain('ml-auto');
    // 行本身得是满宽的 flex，ml-auto 才有可推的空间
    expect(html).toContain('flex w-full');
    // 「思考」在前、耗时在后：先读到标题，再读到耗时
    expect(html.indexOf('思考')).toBeLessThan(html.indexOf('15s'));
  });

  it('没有耗时数据时整段不渲染，不显示 0s', () => {
    const html = render(text, 'standard', false, 0);
    expect(html).not.toContain('>0s<');
  });
});
