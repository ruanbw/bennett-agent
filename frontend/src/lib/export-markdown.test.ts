/**
 * 导出格式的行为锁。
 *
 * 导出是「用户唯一能带出数据的出口」，格式一旦松了就会静默丢信息：失败的回复被导出成
 * 正常回答、工具结果截断后没标注、围栏被正文里的反引号提前闭合。这些都是纯字符串，
 * 用快照式断言直接锁住。
 */

import { describe, expect, it } from 'vitest';

import type { MessageRecord, SessionRecord, ToolCallRecord } from '../types';
import {
  MAX_FILENAME_CHARS,
  MAX_TOOL_TEXT_CHARS,
  archiveFileName,
  buildArchiveMarkdown,
  buildConversationMarkdown,
  downloadMarkdownFile,
  exportFileName,
  fencedBlock,
  formatTimestamp,
  type ArchiveEntry,
} from './export-markdown';

/** 本地时区的固定时刻：断言不依赖运行环境的 TZ。 */
const at = (hour: number, minute = 0): number => new Date(2026, 9, 21, hour, minute).getTime();

const session: Pick<SessionRecord, 'title' | 'createdAt' | 'updatedAt'> = {
  title: '向量数据库选型',
  createdAt: at(9),
  updatedAt: at(11),
};

const userMessage = (overrides: Partial<MessageRecord> = {}): MessageRecord => ({
  id: 'm1',
  sessionId: 's1',
  role: 'user',
  content: '延迟太高了，向量检索一次要 800ms',
  createdAt: at(10),
  ...overrides,
});

const assistantMessage = (overrides: Partial<MessageRecord> = {}): MessageRecord => ({
  id: 'm2',
  sessionId: 's1',
  role: 'assistant',
  content: '先看索引类型，HNSW 通常比 IVF_FLAT 快。',
  createdAt: at(10, 1),
  ...overrides,
});

const toolCall = (overrides: Partial<ToolCallRecord> = {}): ToolCallRecord => ({
  toolCallId: 't1',
  toolName: 'search_docs',
  argsText: '{"q":"HNSW"}',
  result: '找到 2 条文档',
  status: 'complete',
  ...overrides,
});

const build = (messages: MessageRecord[]): string =>
  buildConversationMarkdown(session, messages, { exportedAt: at(12) });

describe('formatTimestamp', () => {
  it('本地时区的 YYYY-MM-DD HH:mm', () => {
    expect(formatTimestamp(at(9, 5))).toBe('2026-10-21 09:05');
  });

  it('非法时间给占位文案而不是 NaN', () => {
    expect(formatTimestamp(Number.NaN)).toBe('时间未知');
  });
});

describe('exportFileName', () => {
  it('去掉路径分隔符与 Windows 保留字符', () => {
    expect(exportFileName('a/b\\c:d*e?f"g<h>i|j')).toBe('a-b-c-d-e-f-g-h-i-j.md');
  });

  it('折叠空白并保留中文标题', () => {
    expect(exportFileName('  向量   检索  ')).toBe('向量 检索.md');
  });

  it('超长标题截断到上限', () => {
    const name = exportFileName('长'.repeat(200));
    expect(name).toBe(`${'长'.repeat(MAX_FILENAME_CHARS)}.md`);
  });

  it('空标题退回带时间戳的默认名（否则所有导出都叫 .md）', () => {
    expect(exportFileName('   ', at(12))).toBe('对话-20261021-1200.md');
  });
});

describe('fencedBlock', () => {
  it('普通内容用三反引号', () => {
    expect(fencedBlock('hello')).toBe('```\nhello\n```');
  });

  it('正文里带反引号时围栏加长（否则代码块会提前闭合）', () => {
    const block = fencedBlock('a ``` b');
    expect(block.startsWith('````\n')).toBe(true);
    expect(block.endsWith('\n````')).toBe(true);
  });
});

describe('buildConversationMarkdown', () => {
  it('文件头带标题、时间与消息数', () => {
    const markdown = build([userMessage(), assistantMessage()]);

    expect(markdown.startsWith('# 向量数据库选型\n')).toBe(true);
    expect(markdown).toContain('> 创建时间：2026-10-21 09:00');
    expect(markdown).toContain('> 最后更新：2026-10-21 11:00');
    expect(markdown).toContain('> 导出时间：2026-10-21 12:00');
    expect(markdown).toContain('> 消息数：2');
  });

  it('按传入顺序渲染，角色标记为「你 / 助手」', () => {
    const markdown = build([userMessage(), assistantMessage()]);
    const userIndex = markdown.indexOf('## 你 · 2026-10-21 10:00');
    const assistantIndex = markdown.indexOf('## 助手 · 2026-10-21 10:01');

    expect(userIndex).toBeGreaterThan(-1);
    expect(assistantIndex).toBeGreaterThan(userIndex);
    expect(markdown).toContain('延迟太高了');
    expect(markdown).toContain('HNSW 通常比 IVF_FLAT 快');
  });

  it('待答提问渲染成引用块，选项带序号', () => {
    const markdown = build([
      assistantMessage({
        content: '',
        clarification: {
          toolCallId: 'ask-1',
          question: '要 A 还是 B？',
          options: ['A 方案', 'B 方案'],
          allowCustomInput: true,
        },
      }),
    ]);

    expect(markdown).toContain('> **提问：** 要 A 还是 B？');
    expect(markdown).toContain('> 1. A 方案');
    expect(markdown).toContain('> 2. B 方案');
  });

  it('生成被停止 / 回复不完整 / 备用模型 / 失败都写进文件', () => {
    const stopped = build([assistantMessage({ stopped: true, incomplete: false })]);
    expect(stopped).toContain('生成被用户停止');

    const incomplete = build([assistantMessage({ stopped: false, incomplete: true })]);
    expect(incomplete).toContain('回复不完整');

    // 两个标记同时存在时只写「用户停止」：已停止的解释更具体，重复写会误导
    const both = build([assistantMessage({ stopped: true, incomplete: true })]);
    expect(both).toContain('生成被用户停止');
    expect(both).not.toContain('回复不完整');

    const fallback = build([
      assistantMessage({ fallback: { fromModel: 'a', toModel: 'b' } }),
    ]);
    expect(fallback).toContain('备用模型完成：a → b');

    const failed = build([
      assistantMessage({
        content: '',
        error: { message: '上游超时', retryable: true, code: null },
      }),
    ]);
    expect(failed).toContain('本轮失败：上游超时');
  });

  it('不导出思考链（默认折叠、体积不可控）', () => {
    const markdown = build([assistantMessage({ thinking: '这里是思考链内部草稿' })]);
    expect(markdown).not.toContain('思考链内部草稿');
  });

  it('工具调用渲染成折叠块，参数与结果都在', () => {
    const markdown = build([assistantMessage({ toolCalls: [toolCall()] })]);

    expect(markdown).toContain('<summary>工具调用 · search_docs · 完成</summary>');
    expect(markdown).toContain('{"q":"HNSW"}');
    expect(markdown).toContain('找到 2 条文档');
  });

  it('运行中的工具调用标成「进行中」', () => {
    const markdown = build([
      assistantMessage({ toolCalls: [toolCall({ status: 'running', result: undefined })] }),
    ]);
    expect(markdown).toContain('search_docs · 进行中');
  });

  it('失败 / 被拒绝的工具调用不冒充完成', () => {
    const failed = build([
      assistantMessage({
        toolCalls: [toolCall({ status: 'complete', outcome: 'failed', result: '工具执行失败' })],
      }),
    ]);
    expect(failed).toContain('search_docs · 失败');

    const denied = build([
      assistantMessage({
        toolCalls: [toolCall({ status: 'complete', outcome: 'denied', result: '被拒绝' })],
      }),
    ]);
    expect(denied).toContain('search_docs · 被拒绝');
  });

  it('超长工具参数截断并标注原文长度', () => {
    const args = 'x'.repeat(MAX_TOOL_TEXT_CHARS + 500);
    const markdown = build([assistantMessage({ toolCalls: [toolCall({ argsText: args })] })]);

    expect(markdown).toContain(`…（已截断，原文 ${args.length} 字符）`);
    // 截断后的内容而不是整段 1500 字符
    expect(markdown).not.toContain(args);
  });

  it('空消息（占位符）不占用消息数', () => {
    const markdown = build([userMessage(), assistantMessage({ content: '' })]);

    expect(markdown).toContain('> 消息数：1');
    expect(markdown).not.toContain('## 助手');
  });

  it('没有消息时给出明确说明', () => {
    const markdown = build([]);
    expect(markdown).toContain('（这条会话还没有可导出的消息。）');
  });

  it('标题为空时用「未命名对话」', () => {
    const markdown = buildConversationMarkdown(
      { title: '   ', createdAt: at(9), updatedAt: at(9) },
      [userMessage()],
    );
    expect(markdown.startsWith('# 未命名对话')).toBe(true);
  });
});

describe('downloadMarkdownFile', () => {
  it('没有 DOM 时安全地返回 false（不在浏览器里就不尝试）', () => {
    expect(downloadMarkdownFile('a.md', '# a')).toBe(false);
  });
});

const entry = (
  title: string,
  messages: MessageRecord[],
  updatedAt = at(11),
): ArchiveEntry => ({
  session: { title, createdAt: at(9), updatedAt },
  messages,
});

describe('buildArchiveMarkdown', () => {
  it('归档头带会话数，每段会话标题降为二级、角色标题降为三级', () => {
    const markdown = buildArchiveMarkdown(
      [entry('会话一', [userMessage()]), entry('会话二', [assistantMessage()])],
      { exportedAt: at(12) },
    );

    expect(markdown.startsWith('# Bennett 对话导出\n')).toBe(true);
    expect(markdown).toContain('> 导出时间：2026-10-21 12:00');
    expect(markdown).toContain('> 会话数：2');
    expect(markdown).toContain('## 会话一');
    expect(markdown).toContain('### 你 · 2026-10-21 10:00');
    expect(markdown).toContain('## 会话二');
    expect(markdown).toContain('### 助手 · 2026-10-21 10:01');
    // 一级的角色标题只属于单会话文件
    expect(markdown).not.toMatch(/^## (你|助手) /m);
  });

  it('每段会话带一行紧凑元信息（不重复品牌头）', () => {
    const markdown = buildArchiveMarkdown([entry('会话一', [userMessage()], at(11))], {
      exportedAt: at(12),
    });

    expect(markdown).toContain('> 最后更新：2026-10-21 11:00 · 消息数：1');
    // 品牌行只在归档头出现一次
    expect(markdown.match(/导出自 Bennett Agent/g)).toHaveLength(1);
  });

  it('空会话也保留在归档里，并与下一段分隔开', () => {
    const markdown = buildArchiveMarkdown(
      [entry('空会话', []), entry('有内容', [userMessage()])],
      { exportedAt: at(12) },
    );

    expect(markdown).toContain('（这条会话还没有可导出的消息。）');
    // 空会话段落之后必须有分隔线，否则下一段标题会粘在上面
    expect(markdown.indexOf('---', markdown.indexOf('## 空会话'))).toBeLessThan(
      markdown.indexOf('## 有内容'),
    );
  });

  it('没有任何会话时给出明确说明', () => {
    const markdown = buildArchiveMarkdown([], { exportedAt: at(12) });
    expect(markdown).toContain('> 会话数：0');
    expect(markdown).toContain('（没有可导出的会话。）');
  });
});

describe('archiveFileName', () => {
  it('带时间戳、无空格、可排序', () => {
    expect(archiveFileName(at(12))).toBe('Bennett-对话导出-20261021-1200.md');
  });
});
