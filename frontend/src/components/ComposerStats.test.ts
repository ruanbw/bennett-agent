import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import type { ThreadMessageLike } from '@assistant-ui/react';

import { ComposerStats } from './ComposerStats';
import type { StatsMessageLike } from '@/lib/session-stats';
import type { UsageTiming } from '@/types';

/**
 * 统计药丸的渲染契约（SSR，仓库没有 jsdom）。
 *
 * 这里守的是「不能编数字」那几条：拿不到就是整颗药丸不出现，而不是 0。
 * 改样式时最容易被顺手改坏的就是它们。
 */
const render = (props: Partial<Parameters<typeof ComposerStats>[0]> = {}) =>
  renderToStaticMarkup(createElement(ComposerStats, { messages: [], ...props }));

const timing = (overrides: Partial<UsageTiming> = {}): UsageTiming => ({
  steps: 3,
  turnMs: 9_000,
  llmMs: 6_000,
  toolMs: 2_000,
  ttftMs: 400,
  decodeMs: 2_000,
  decodeTokens: 400,
  ...overrides,
});

const usage = (overrides: Record<string, unknown> = {}): Record<string, unknown> => ({
  contextTokens: 2_000,
  contextWindow: 128_000,
  contextLimitTokens: 100_000,
  outputTokens: 500,
  cacheReadTokens: 1_600,
  ...overrides,
});

const assistant = (custom: Record<string, unknown>): StatsMessageLike => ({
  role: 'assistant',
  metadata: { custom },
});

describe('ComposerStats：没有数据时整颗药丸不渲染', () => {
  it('空会话什么都不渲染', () => {
    expect(render()).toBe('');
  });

  it('有 usage 但没有 timing 时只少 activity 药丸，不显示 0 tok/s', () => {
    const html = render({ messages: [assistant({ usage: usage() })] });

    expect(html).toContain('data-composer-stat="usage"');
    expect(html).not.toContain('data-composer-stat="activity"');
    // 没有任何一轮带计时 → 不能出现「0 轮 0 步」或「0 tok/s」
    expect(html).not.toContain('0 tok/s');
    expect(html).not.toContain('0 轮');
  });

  it('有 timing 但账单为 0 时只少 usage 药丸，不显示「缓存命中 0%」', () => {
    const html = render({
      messages: [
        assistant({
          usage: usage({ contextTokens: 0, outputTokens: 0, cacheReadTokens: 0, timing: timing() }),
        }),
      ],
    });

    expect(html).toContain('data-composer-stat="activity"');
    expect(html).not.toContain('data-composer-stat="usage"');
    expect(html).not.toContain('缓存命中 0%');
  });

  it('steps === 0 的计时块整块无效，activity 不出现', () => {
    // steps=0 时 readUsageTiming 仍然返回对象，但轮次/步数为 0 的药丸没有意义
    const html = render({
      messages: [assistant({ usage: usage({ timing: timing({ steps: 0 }) }) })],
    });

    expect(html).not.toContain('data-composer-stat="activity"');
  });
});

describe('ComposerStats：读数来自真实数据', () => {
  it('activity 药丸显示轮次、步数与解码阶段速度', () => {
    const html = render({
      messages: [
        assistant({ usage: usage({ timing: timing() }) }),
        assistant({ usage: usage({ timing: timing({ steps: 2 }) }) }),
      ],
    });

    expect(html).toContain('2 轮 5 步');
    // 两轮各 400 tok / 2s → 800 tok / 4s = 200 tok/s
    expect(html).toContain('200 tok/s');
  });

  it('速度的分母是解码阶段，不是整轮墙钟', () => {
    const html = render({
      messages: [
        assistant({
          usage: usage({
            // 整轮 90s，但解码只用了 2s：速度必须是 200，不是 4
            timing: timing({ turnMs: 90_000, decodeMs: 2_000, decodeTokens: 400 }),
          }),
        }),
      ],
    });

    expect(html).toContain('200 tok/s');
    expect(html).not.toContain('4 tok/s');
  });

  it('usage 药丸显示总量与缓存命中率（分母是三个 prompt 桶之和）', () => {
    const html = render({
      messages: [assistant({ usage: usage({ cacheReadTokens: 1_600 }) })],
    });

    // 1,600 命中 / 2,000 计费输入 = 80%；总量 2,000 + 500 = 2.5k
    expect(html).toContain('2.5k tok');
    expect(html).toContain('缓存命中 80%');
  });

  it('一次都没命中时如实显示 0%（这是真实测量值，不是缺数据）', () => {
    const html = render({
      messages: [assistant({ usage: usage({ cacheReadTokens: 0 }) })],
    });

    expect(html).toContain('2.5k tok');
    expect(html).toContain('缓存命中 0%');
  });

  it('上下文圆环读最近一轮的用量', () => {
    const html = render({
      messages: [
        assistant({ usage: usage({ contextTokens: 1_000 }) }),
        assistant({ usage: usage({ contextTokens: 25_000, contextLimitTokens: 100_000 }) }),
      ],
      currentModel: 'auto/x',
    });

    // 最近一轮 25k / 100k = 25%
    expect(html).toContain('25%');
  });
});

describe('ComposerStats：简洁档', () => {
  it('只留输出速度与缓存命中率，且都不弹面板', () => {
    const html = render({
      messages: [assistant({ usage: usage({ timing: timing() }) })],
      performanceUsage: 'compact',
    });

    expect(html).toContain('200 tok/s');
    expect(html).toContain('缓存命中 80%');
    // 轮次步数与总量在这档不显示
    expect(html).not.toContain('1 轮');
    expect(html).not.toContain('2.5k tok');
    // 这两个药丸不可点（上下文档的圆环仍可点，所以只断言没有统计面板的标题）
    expect(html).not.toContain('会话统计');
    expect(html).not.toContain('Token 用量');
  });

  it('上下文圆环不受档位影响，始终显示', () => {
    const html = render({
      messages: [assistant({ usage: usage({ contextTokens: 25_000, contextLimitTokens: 100_000 }) })],
      performanceUsage: 'compact',
      currentModel: 'auto/x',
    });

    expect(html).toContain('25%');
  });
});

describe('ComposerStats：三个面板', () => {
  it('activity 面板列出模型用时、工具用时、TTFT 与输出速度', () => {
    const html = render({
      messages: [assistant({ usage: usage({ timing: timing() }) })],
      initialOpen: 'activity',
    });

    expect(html).toContain('会话统计');
    expect(html).toContain('模型用时');
    expect(html).toContain('工具调用用时');
    expect(html).toContain('首 token 平均（TTFT）');
    expect(html).toContain('输出速度（TPS）');
    // 时长紧凑格式：6s / 2s / 0.4s
    expect(html).toContain('6秒');
    expect(html).toContain('0.4秒');
  });

  it('activity 面板的时长超过一分钟写成「M分S秒」', () => {
    const html = render({
      messages: [assistant({ usage: usage({ timing: timing({ llmMs: 162_000 }) }) })],
      initialOpen: 'activity',
    });

    expect(html).toContain('2分42秒');
  });

  it('usage 面板给出精确数字（千位分隔），不是缩写', () => {
    const html = render({
      messages: [
        assistant({
          usage: usage({ contextTokens: 1_234_567, outputTokens: 500_000, cacheReadTokens: 1_000_000 }),
        }),
      ],
      initialOpen: 'usage',
    });

    expect(html).toContain('Token 用量');
    // 未缓存输入 = prompt 1,234,567 - 命中 1,000,000
    expect(html).toContain('234,567');
    expect(html).toContain('1,000,000');
    expect(html).toContain('未缓存输入');
    expect(html).toContain('缓存读取');
    expect(html).toContain('输出');
    // 精确总量在标题右上角
    expect(html).toContain('1,734,567 tok');
  });

  it('缓存写入未知时不渲染那一行（本项目上游不回报它）', () => {
    const html = render({
      messages: [assistant({ usage: usage() })],
      initialOpen: 'usage',
    });

    expect(html).not.toContain('缓存写入');
  });

  it('面板是 role="dialog" 且带 aria-label', () => {
    const activity = render({
      messages: [assistant({ usage: usage({ timing: timing() }) })],
      initialOpen: 'activity',
    });
    expect(activity).toContain('role="dialog"');
    expect(activity).toContain('aria-label="会话统计"');

    const usagePanel = render({
      messages: [assistant({ usage: usage() })],
      initialOpen: 'usage',
    });
    expect(usagePanel).toContain('aria-label="Token 用量"');
  });

  it('药丸按钮带 aria-haspopup / aria-expanded（可点开才写）', () => {
    const html = render({
      messages: [assistant({ usage: usage({ timing: timing() }) })],
    });

    expect(html).toContain('aria-haspopup="dialog"');
    expect(html).toContain('aria-expanded="false"');
  });
});

describe('ComposerStats：上下文分段条', () => {
  it('没有分段数据时退化成单色整条，不编造三段比例', () => {
    const html = render({
      messages: [assistant({ usage: usage({ contextTokens: 25_000, contextLimitTokens: 100_000 }) })],
      currentModel: 'auto/x',
      initialContextOpen: true,
    });

    expect(html).toContain('aria-label="上下文占用"');
    // 图例是分段的证据：没有分段数据就不该出现（注意「系统提示词」这个词在说明
    // 正文里也会出现，所以只断言图例独有的两个标签与图例色块）
    expect(html).not.toContain('工具定义');
    expect(html).not.toContain('对话消息');
    expect(html).not.toContain('bg-[#a78bfa]');
    expect(html).toContain('服务端还没有回报');
  });

  it('有分段数据时画三段并给出图例', () => {
    const html = render({
      messages: [assistant({ usage: usage({ contextTokens: 25_000, contextLimitTokens: 100_000 }) })],
      currentModel: 'auto/x',
      breakdown: { systemTokens: 5_000, toolsTokens: 10_000, messageTokens: 10_000 },
      initialContextOpen: true,
    });

    expect(html).toContain('系统提示词');
    expect(html).toContain('工具定义');
    expect(html).toContain('对话消息');
  });
});

describe('ComposerStats：锁定态', () => {
  it('锁定态显示「已满」并给出发不出去的解释', () => {
    const html = render({
      messages: [assistant({ usage: usage() })],
      locked: true,
    });

    expect(html).toContain('已满');
    expect(html).toContain('上下文已满，查看说明');
  });
});

describe('ComposerStats：接线契约', () => {
  it('直接接受 assistant-ui 的 ThreadMessageLike[]（Lead 不用先转换数组）', () => {
    // 类型层面：thread.messages 是 `ThreadMessageLike[]`，而 selector 里做 map 会返回
    // 新数组 → 快照永远不相等 → 组件无限重渲染。所以 props 必须能吃原始数组。
    const runtimeMessages: readonly ThreadMessageLike[] = [
      {
        role: 'assistant',
        content: [{ type: 'text', text: '答' }],
        metadata: { custom: { usage: usage({ timing: timing() }), model: 'auto/x' } },
      },
    ];
    const html = renderToStaticMarkup(
      createElement(ComposerStats, { messages: runtimeMessages, currentModel: 'auto/x' }),
    );

    expect(html).toContain('1 轮 3 步');
  });
});

describe('ComposerStats：同一时刻只开一个面板', () => {
  it('initialOpen 只开指定的那一个（三个 fixed 弹层不会叠在一起）', () => {
    const html = render({
      messages: [assistant({ usage: usage({ timing: timing() }) })],
      currentModel: 'auto/x',
      initialOpen: 'activity',
      initialContextOpen: true,
    });

    expect(html).toContain('aria-label="会话统计"');
    // 圆环的弹层没开：initialOpen 优先，同一时刻只有一个
    expect(html).not.toContain('aria-label="上下文用量详情"');
  });
});
