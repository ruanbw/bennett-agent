/**
 * 澄清弹层的静态结构锁。
 *
 * 交互（提交一次、限高滚动、忽略后焦点回输入框）由 Playwright 覆盖；这里锁那些
 * 只靠纯函数测不到、改样式/分支时最容易丢的行为：清洗后的选项数量、展开入口、
 * 以及「没有有效选项时仍必须给自定义输入框」这条兜底。
 */

import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { ClarificationInputPopup } from './clarification-input-popup';

function render(options: string[], allowCustomInput: boolean) {
  return renderToStaticMarkup(
    createElement(ClarificationInputPopup, {
      clarification: { question: '你想要哪一种结果？', options, allowCustomInput },
      onSubmitAnswer: vi.fn(),
    }),
  );
}

describe('ClarificationInputPopup', () => {
  it('渲染问题与清洗后的选项；不允许自定义时不出现输入框', () => {
    const html = render(['方案 A', ' 方案 B ', ''], false);

    expect(html).toContain('你想要哪一种结果？');
    expect(html).toContain('方案 A');
    expect(html).toContain('方案 B');
    // 两个选项按钮；没有关闭/展开/发送按钮（测试不传 onDismiss）
    expect(html.match(/<button/g)).toHaveLength(2);
    expect(html).not.toContain('aria-label="自定义输入"');
  });

  it('超过 8 个选项时给出展开入口，但不把全部按钮一次铺开', () => {
    const options = Array.from({ length: 10 }, (_, i) => `选项${i}`);
    const html = render(options, false);

    expect(html).toContain('展开更多（还有 2 个）');
    expect(html).toContain('aria-expanded="false"');
    expect(html).toContain('aria-controls=');
    expect(html).not.toContain('收起选项');
    expect(html.match(/<button/g)).toHaveLength(9);
    expect(html).not.toContain('选项9');
  });

  it('正好 8 个选项时不出现展开入口', () => {
    const options = Array.from({ length: 8 }, (_, i) => `选项${i}`);
    const html = render(options, false);

    expect(html).not.toContain('展开更多');
    expect(html.match(/<button/g)).toHaveLength(8);
  });

  it('清洗后没有有效选项时，即便后端禁止自定义也要给输入框', () => {
    const html = render(['', '   ', ''], false);

    expect(html).toContain('aria-label="自定义输入"');
    expect(html).toContain('输入你的答案并回车...');
  });

  it('有选项时自定义输入用「或」说明它是备选路径', () => {
    const html = render(['方案 A', '方案 B'], true);

    expect(html).toContain('aria-label="自定义输入"');
    expect(html).toContain('或手动输入具体要求并回车...');
  });
});
