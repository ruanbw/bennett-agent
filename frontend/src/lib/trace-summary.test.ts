import { describe, expect, it } from 'vitest';

import {
  describeCompletedTurn,
  describeToolAction,
  describeToolActions,
  describeTraceSummary,
  liveToolDetail,
  reasoningPreview,
  traceGroupHeader,
  traceToggleLabel,
} from './trace-summary';

describe('工具动作文案', () => {
  it('走与组头同一张分类表：认得出的工具给出过去式动作', () => {
    expect(describeToolAction('bash')).toBe('执行了命令');
    expect(describeToolAction('read')).toBe('已读取文件');
    expect(describeToolAction('web_search')).toBe('已搜索网页');
    expect(describeToolAction('tavily_search')).toBe('已搜索网页');
    expect(describeToolAction('ask_user_clarification')).toBe('向用户提出了问题');
  });

  it('认不出的工具说「已调用工具」，不把内部名字甩给用户', () => {
    expect(describeToolAction('some_unknown_thing')).toBe('已调用工具');
    expect(describeToolAction('')).toBe('已调用工具');
  });

  it('一串工具名按 DSH 规则拼成一句（两项用「并」、三项用逗号、超三类加「等」）', () => {
    expect(describeToolActions(['bash', 'bash'])).toBe('执行了命令');
    expect(describeToolActions(['read', 'write'])).toBe('已读取文件并写入文件');
    expect(describeToolActions(['read', 'grep', 'bash'])).toBe(
      '已读取文件，已搜索代码，执行了命令'
    );
    expect(describeToolActions(['read', 'write', 'bash', 'web_search'])).toBe(
      '已读取文件，已写入文件，执行了命令等'
    );
    expect(describeToolActions([])).toBe('');
  });

  it('不再出现「等 N 项」这种计数：用户要知道干了什么，不是干了几次', () => {
    const text = describeToolActions(['read', 'write', 'bash', 'web_search']);
    expect(text).not.toMatch(/\d+\s*项/);
    expect(text).not.toMatch(/\d+\s*次/);
  });
});

describe('「过程」折叠条文案', () => {
  it('只说动作，不含耗时——耗时属于整轮的时长行', () => {
    const text = describeTraceSummary({ thinkingSeconds: 99, toolNames: ['bash', 'read'] });
    // 首项「执行了命令」不以「已」开头，所以第二项保留「已」——规则是「两项都以已开头才去掉」
    expect(text).toBe('执行了命令并已读取文件');
    expect(text).not.toContain('已思考');
    expect(text).not.toContain('99');
  });

  it('只有思考（没有工具）时给中性文案，不编造动作', () => {
    expect(describeTraceSummary({ thinkingSeconds: 3 })).toBe('过程');
  });

  it('只有工具时就是工具动作', () => {
    expect(describeTraceSummary({ toolNames: ['tavily_search'] })).toBe('已搜索网页');
  });

  it('失败次数单独标出来：折叠着也要看出这轮不顺利', () => {
    const text = describeTraceSummary({ toolNames: ['tavily_search'], failedCount: 1 });
    expect(text).toBe('已搜索网页，1 个失败');
  });

  it('什么都没有时给中性文案，不返回空串（空标题的折叠条没有可点目标）', () => {
    expect(describeTraceSummary({})).toBe('过程');
    expect(describeTraceSummary({ running: true })).toBe('正在处理…');
  });
});

describe('折叠条的读屏文案', () => {
  it('带上是展开还是收起，以及展开的是什么', () => {
    const summary = describeTraceSummary({ toolNames: ['bash'] });
    expect(traceToggleLabel(summary, false)).toBe('展开过程：执行了命令');
    expect(traceToggleLabel(summary, true)).toBe('收起过程：执行了命令');
  });
});

describe('思考行的预览摘要', () => {
  it('已结束取首行，不带后面的段落', () => {
    expect(reasoningPreview('先看代码结构\n然后改实现', false)).toBe('先看代码结构');
  });

  it('运行中取最后一个已完成段落的首行——正在写的半句话不当预览', () => {
    const streaming = '第一段首行\n第一段第二行\n\n第二段首行\n第二段第二行\n\n正在敲的这半句';
    // 最后一个有换行的段落是「第二段」，正在敲的那段没有换行所以被跳过
    expect(reasoningPreview(streaming, true)).toBe('第二段首行');
  });

  it('去掉 ** 粗体标记：单行纯文本里星号只是噪音', () => {
    expect(reasoningPreview('**结论**：方案可行', false)).toBe('结论：方案可行');
    expect(reasoningPreview('**结论**：方案可行\n第二行', true)).toBe('结论：方案可行');
  });

  it('没有文本、或运行中还一段都没写完时返回空串（调用方据此隐藏摘要）', () => {
    expect(reasoningPreview(undefined, false)).toBe('');
    expect(reasoningPreview('', false)).toBe('');
    expect(reasoningPreview('只有一行还没换行', true)).toBe('');
  });

  it('空段落不参与挑选：不会把空行当预览', () => {
    expect(reasoningPreview('第一段\n\n  \n\n第二段', true)).toBe('第一段');
  });
});

describe('运行中组头的详情行', () => {
  it('取第一个有内容的主参数（命令、路径、查询词…）', () => {
    expect(liveToolDetail('{"command":"npm test","description":"跑测试"}')).toBe('跑测试');
    expect(liveToolDetail('{"file_path":"/tmp/a.ts"}')).toBe('/tmp/a.ts');
    expect(liveToolDetail('{"query":"dsh 折叠"}')).toBe('dsh 折叠');
  });

  it('参数还在流式生成（不是合法 JSON）时返回空串，不显示半截 {', () => {
    expect(liveToolDetail('{"query":"半')).toBe('');
    expect(liveToolDetail('')).toBe('');
    expect(liveToolDetail(undefined)).toBe('');
  });

  it('数组参数取第一个可读项，对象数组取其中的 question', () => {
    expect(liveToolDetail('{"queries":["第一个词","第二个词"]}')).toBe('第一个词');
    expect(liveToolDetail('{"questions":[{"question":"要不要删库？"}]}')).toBe('要不要删库？');
  });

  it('折成一行并截断超长描述', () => {
    const long = 'x'.repeat(300);
    const detail = liveToolDetail(JSON.stringify({ command: `a\nb  ${long}` }));
    expect(detail.startsWith('a b')).toBe(true);
    expect(detail.endsWith('…')).toBe(true);
    expect(Array.from(detail).length).toBeLessThanOrEqual(160);
  });
});

describe('整轮的一句话摘要', () => {
  it('动作 + 失败数 + 用时，按顺序用逗号连接', () => {
    expect(
      describeCompletedTurn({ toolNames: ['read', 'bash'], failedCount: 1, elapsedMs: 65_000 })
    ).toBe('已读取文件并执行了命令，1 个失败，1分5秒');
  });

  it('没有耗时数据时不编一个 0', () => {
    expect(describeCompletedTurn({ toolNames: ['bash'], elapsedMs: null })).toBe('执行了命令');
    expect(describeCompletedTurn({})).toBe('');
  });
});

describe('过程组组头（运行中/已结束 × 详情 × 失败）', () => {
  it('运行中且档位允许时带详情', () => {
    const header = traceGroupHeader({
      running: true,
      activity: 'commands',
      detail: 'npm test',
      showLiveDetail: true,
    });
    expect(header.title).toBe('正在运行命令 · npm test');
    expect(header.summary).toBe('正在运行命令');
    expect(header.detail).toBe('npm test');
  });

  it('运行中但档位不允许（简洁档）时只有动作', () => {
    const header = traceGroupHeader({
      running: true,
      activity: 'commands',
      detail: 'npm test',
      showLiveDetail: false,
    });
    expect(header.title).toBe('正在运行命令');
    expect(header.detail).toBe('');
  });

  it('已结束时不给详情，即使传了也不显示', () => {
    const header = traceGroupHeader({
      running: false,
      activity: 'commands',
      detail: 'npm test',
      toolNames: ['read', 'bash'],
      showLiveDetail: true,
    });
    // 两项用「并」连接，不是逗号：`已读取文件并执行了命令`
    expect(header.title).toBe('已读取文件并执行了命令');
    expect(header.detail).toBe('');
  });

  it('失败数追加在最后，折叠着也能看出这轮不顺利', () => {
    const header = traceGroupHeader({
      running: false,
      activity: 'thinking',
      toolNames: ['bash'],
      failedCount: 2,
    });
    expect(header.title).toBe('执行了命令');
    expect(header.failureNote).toBe('，2 个失败');
  });

  it('运行中但有失败调用时同样标出失败数', () => {
    const header = traceGroupHeader({
      running: true,
      activity: 'read',
      toolNames: ['bash', 'read'],
      failedCount: 1,
    });
    expect(header.title).toBe('正在读取文件');
    expect(header.failureNote).toBe('，1 个失败');
  });
});
