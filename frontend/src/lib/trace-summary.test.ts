import { describe, expect, it } from 'vitest';

import {
  describeToolAction,
  describeToolActions,
  describeTraceSummary,
  traceToggleLabel,
} from './trace-summary';

describe('工具动作文案', () => {
  it('登记过的工具翻成动作短语，没登记的按前缀兜底', () => {
    expect(describeToolAction('tavily_search')).toBe('联网搜索');
    expect(describeToolAction('get_current_time')).toBe('查询了时间');
    expect(describeToolAction('bash')).toBe('执行了命令');
    expect(describeToolAction('read_file')).toBe('已读取文件');
  });

  it('MCP 工具取分隔符之后那段再判断（`mcp__github__read_file`）', () => {
    expect(describeToolAction('mcp__github__read_file')).toBe('已读取文件');
    expect(describeToolAction('mcp__fs__write_file')).toBe('已写入文件');
  });

  it('只认识前缀时按前缀给动作，完全认不出才说「已调用工具」', () => {
    expect(describeToolAction('read_spreadsheet')).toBe('已读取文件');
    expect(describeToolAction('search_docs')).toBe('搜索');
    expect(describeToolAction('some_unknown_thing')).toBe('已调用工具');
    expect(describeToolAction('')).toBe('已调用工具');
  });

  it('动作去重后按调用顺序拼接，超出上限用「等」收口', () => {
    expect(describeToolActions(['bash', 'bash'])).toBe('执行了命令');
    expect(describeToolActions(['bash', 'read_file'])).toBe('执行了命令，已读取文件');
    expect(describeToolActions(['bash', 'read_file', 'write_file', 'search_docs', 'grep'])).toBe(
      '执行了命令，已读取文件，已写入文件 等 5 项'
    );
    expect(describeToolActions([])).toBe('');
  });
});

describe('「过程」折叠条文案', () => {
  it('思考与工具动作合成一句', () => {
    expect(
      describeTraceSummary({ thinkingSeconds: 2, toolNames: ['bash', 'read_file'] })
    ).toBe('已思考 2s · 执行了命令，已读取文件');
  });

  it('只有思考时只说思考，只有工具时只说工具', () => {
    expect(describeTraceSummary({ thinkingSeconds: 3 })).toBe('已思考 3s');
    expect(describeTraceSummary({ toolNames: ['tavily_search'] })).toBe('联网搜索');
  });

  it('失败次数单独标出来：折叠着也要看出这轮不顺利', () => {
    const text = describeTraceSummary({
      thinkingSeconds: 1,
      toolNames: ['tavily_search'],
      failedCount: 1,
    });
    expect(text).toContain('1 个失败');
  });

  it('什么都没有时给中性文案，不返回空串（空标题的折叠条没有可点目标）', () => {
    expect(describeTraceSummary({})).toBe('过程');
    expect(describeTraceSummary({ running: true })).toBe('正在处理…');
  });
});

describe('折叠条的读屏文案', () => {
  it('带上是展开还是收起，以及展开的是什么', () => {
    const summary = describeTraceSummary({ thinkingSeconds: 2 });
    expect(traceToggleLabel(summary, false)).toBe('展开过程：已思考 2s');
    expect(traceToggleLabel(summary, true)).toBe('收起过程：已思考 2s');
  });
});
