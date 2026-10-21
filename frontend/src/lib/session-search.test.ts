/**
 * 会话正文搜索的行为锁。
 *
 * 两类用例：
 * 1. 纯逻辑（词表 / AND 匹配 / 高亮偏移 / 片段截取）——`snippet.text.slice(range)` 必须
 *    恰是命中的词，这是「高亮错位」唯一能提前发现的地方。
 * 2. 真 Dexie 扫库（`fake-indexeddb`）：按会话聚合、片段取最新命中、不搜 thinking、
 *    候选会话之外的消息不被读出来。
 */

import 'fake-indexeddb/auto';

import { beforeEach, describe, expect, it } from 'vitest';

import type { MessageRecord } from '../types';
import { db } from '../db';
import {
  MAX_TOOL_SEARCH_CHARS,
  buildSnippet,
  collapseWhitespace,
  highlightRanges,
  matchText,
  parseSearchQuery,
  searchSessionContent,
} from './session-search';

const message = (
  id: string,
  sessionId: string,
  content: string,
  createdAt: number,
  extra: Partial<MessageRecord> = {},
): MessageRecord => ({ id, sessionId, role: 'user', content, createdAt, ...extra });

describe('parseSearchQuery', () => {
  it('按空白切词并统一小写', () => {
    expect(parseSearchQuery('  向量   检索 ')).toEqual(['向量', '检索']);
    expect(parseSearchQuery('Vector DB')).toEqual(['vector', 'db']);
  });

  it('去重并丢弃空词', () => {
    expect(parseSearchQuery('向量 向量   检索')).toEqual(['向量', '检索']);
    expect(parseSearchQuery('   ')).toEqual([]);
  });

  it('最多取前 8 个词：再多也不会更精确', () => {
    const tokens = parseSearchQuery('a b c d e f g h i j');
    expect(tokens).toHaveLength(8);
    expect(tokens[7]).toBe('h');
  });
});

describe('matchText', () => {
  it('多词是与：缺一个就不命中', () => {
    expect(matchText('向量数据库选型', ['向量', '选型'])).not.toBeNull();
    expect(matchText('向量数据库选型', ['向量', '延迟'])).toBeNull();
  });

  it('大小写不敏感，并统计出现次数', () => {
    const match = matchText('RAG 与 rag 都是 Retrieval', ['rag']);
    expect(match?.count).toBe(2);
    expect(match?.firstPositions).toEqual([0]);
  });

  it('空查询/空文本不算命中', () => {
    expect(matchText('abc', [])).toBeNull();
    expect(matchText('', ['a'])).toBeNull();
  });

  it('首次位置基于原文，不受小写后长度变化影响', () => {
    // İ 的小写是 i + 组合点（长度 +1）：旧的 toLowerCase 偏移会把位置算成 10
    expect(matchText('İstanbul 向量', ['向量'])?.firstPositions).toEqual([9]);
  });
});

describe('highlightRanges', () => {
  it('区间有序、互不重叠，且切片就是命中的词', () => {
    const text = '向量检索和向量库';
    const ranges = highlightRanges(text, ['向量']);
    expect(ranges).toEqual([
      { start: 0, end: 2 },
      { start: 5, end: 7 },
    ]);
    for (const range of ranges) expect(text.slice(range.start, range.end)).toBe('向量');
  });

  it('重叠的词合并成一个区间', () => {
    expect(highlightRanges('abcd', ['abc', 'bcd'])).toEqual([{ start: 0, end: 4 }]);
  });

  it('没有任何命中时返回空数组', () => {
    expect(highlightRanges('无关文本', ['向量'])).toEqual([]);
  });

  it('文本里有小写后变长的字符（İ）也仍然按原文偏移高亮', () => {
    const text = 'İstanbul 向量';

    expect(highlightRanges(text, ['向量'])).toEqual([{ start: 9, end: 11 }]);
    expect(highlightRanges(text, ['向量']).map((range) => text.slice(range.start, range.end))).toEqual([
      '向量',
    ]);
  });

  it('查询词里的正则元字符按字面量匹配', () => {
    expect(highlightRanges('a[b]c', ['[b]'])).toEqual([{ start: 1, end: 4 }]);
  });
});

describe('buildSnippet', () => {
  it('命中在中间时两侧加省略号，空白折叠成单空格', () => {
    const text = `${'铺垫'.repeat(60)}\n\n向量   检索\n${'收尾'.repeat(60)}`;
    const snippet = buildSnippet(text, ['向量']);

    expect(snippet).not.toBeNull();
    expect(snippet!.text.startsWith('…')).toBe(true);
    expect(snippet!.text.endsWith('…')).toBe(true);
    expect(collapseWhitespace(snippet!.text)).toBe(snippet!.text);
    expect(snippet!.text).toContain('向量 检索');
    expect(snippet!.text.length).toBeLessThanOrEqual(122);
  });

  it('高亮区间对得上片段里的命中的词（含省略号偏移）', () => {
    const text = `${'前缀'.repeat(40)} 向量数据库 ${'后缀'.repeat(40)}`;
    const snippet = buildSnippet(text, ['向量'])!;

    expect(snippet.ranges.length).toBeGreaterThan(0);
    for (const range of snippet.ranges) {
      expect(snippet.text.slice(range.start, range.end)).toBe('向量');
    }
  });

  it('多个词都高亮，且任一词缺失就返回 null', () => {
    const snippet = buildSnippet('先聊向量，再聊检索', ['向量', '检索'])!;
    const highlighted = snippet.ranges.map((range) => snippet.text.slice(range.start, range.end));
    expect(highlighted.sort()).toEqual(['向量', '检索'].sort());

    expect(buildSnippet('只聊向量', ['向量', '检索'])).toBeNull();
  });

  it('短文本不加省略号', () => {
    expect(buildSnippet('向量检索', ['向量'])!.text).toBe('向量检索');
  });

  it('空文本或空查询返回 null', () => {
    expect(buildSnippet('   ', ['a'])).toBeNull();
    expect(buildSnippet('abc', [])).toBeNull();
  });
});

describe('searchSessionContent（真 Dexie）', () => {
  beforeEach(async () => {
    await db.messages.clear();
    await db.sessions.clear();
    await db.syncMeta.clear();
  });

  it('按会话聚合：命中条数与片段来自最新一条命中的消息', async () => {
    await db.messages.bulkAdd([
      message('m1', 's1', '先聊聊向量数据库选型', 1),
      message('m2', 's1', '向量检索的延迟太高了', 5),
      message('m3', 's2', '完全无关的内容', 9),
    ]);

    const matches = await searchSessionContent(['向量'], { sessionIds: ['s1', 's2'] });

    expect([...matches.keys()]).toEqual(['s1']);
    expect(matches.get('s1')!.messageCount).toBe(2);
    expect(matches.get('s1')!.newestMatchedAt).toBe(5);
    expect(matches.get('s1')!.snippet.text).toContain('向量检索的延迟');
  });

  it('多词必须在同一条消息里都出现（片段要能自证命中原因）', async () => {
    await db.messages.bulkAdd([
      message('m1', 's1', '先聊向量', 1),
      message('m2', 's1', '再聊检索', 2),
    ]);

    // 两个词分属不同消息：不命中，否则片段里凑不出用户高亮的词
    const crossMessage = await searchSessionContent(['向量', '检索'], { sessionIds: ['s1'] });
    expect(crossMessage.size).toBe(0);

    await db.messages.add(message('m3', 's1', '向量检索一起聊', 3));
    const sameMessage = await searchSessionContent(['向量', '检索'], { sessionIds: ['s1'] });
    expect(sameMessage.get('s1')!.messageCount).toBe(1);

    const missing = await searchSessionContent(['向量', '重排'], { sessionIds: ['s1'] });
    expect(missing.size).toBe(0);
  });

  it('不搜 thinking：命中只存在于思考链时不返回该会话', async () => {
    await db.messages.bulkAdd([
      message('m1', 's1', '正文里没有那个词', 1, { thinking: '思考链里提到了向量' }),
    ]);

    const matches = await searchSessionContent(['向量'], { sessionIds: ['s1'] });
    expect(matches.size).toBe(0);
  });

  it('工具结果参与搜索，并标注来源（工具块默认折叠）', async () => {
    await db.messages.add(
      message('m1', 's1', '', 1, {
        role: 'assistant',
        toolCalls: [
          {
            toolCallId: 't1',
            toolName: 'search_docs',
            argsText: '{"q":"x"}',
            result: '这是一段只在工具结果里出现的独特文本',
            status: 'complete',
          },
        ],
      }),
    );

    const matches = await searchSessionContent(['独特文本'], { sessionIds: ['s1'] });

    expect(matches.get('s1')!.label).toBe('工具 search_docs');
    expect(matches.get('s1')!.snippet.text).toContain('独特文本');
  });

  it('工具名与参数也参与搜索', async () => {
    await db.messages.add(
      message('m1', 's1', '', 1, {
        role: 'assistant',
        toolCalls: [
          {
            toolCallId: 't1',
            toolName: 'web_search',
            argsText: '{"query":"special_arg_token"}',
            result: '结果',
            status: 'complete',
          },
        ],
      }),
    );

    const byName = await searchSessionContent(['web_search'], { sessionIds: ['s1'] });
    expect(byName.get('s1')!.label).toBe('工具 web_search');

    const byArg = await searchSessionContent(['special_arg_token'], { sessionIds: ['s1'] });
    expect(byArg.get('s1')!.label).toBe('工具 web_search');
  });

  it('正文命中优先于工具命中（同一消息里先看正文）', async () => {
    await db.messages.add(
      message('m1', 's1', 'HNSW 在正文里', 1, {
        toolCalls: [
          {
            toolCallId: 't1',
            toolName: 'search_docs',
            argsText: '',
            result: 'HNSW 也在工具结果里',
            status: 'complete',
          },
        ],
      }),
    );

    const matches = await searchSessionContent(['HNSW'], { sessionIds: ['s1'] });

    expect(matches.get('s1')!.label).toBeNull();
    expect(matches.get('s1')!.snippet.text).toContain('正文');
  });

  it('工具结果只搜前 20k 字符，超出部分不命中', async () => {
    const longResult = `${'a'.repeat(MAX_TOOL_SEARCH_CHARS)}尾部独有词`;
    await db.messages.add(
      message('m1', 's1', '', 1, {
        role: 'assistant',
        toolCalls: [
          {
            toolCallId: 't1',
            toolName: 'fetch_page',
            argsText: '',
            result: longResult,
            status: 'complete',
          },
        ],
      }),
    );

    const matches = await searchSessionContent(['尾部独有词'], { sessionIds: ['s1'] });
    expect(matches.size).toBe(0);
  });

  it('待答提问参与搜索（正文为空时也能命中）', async () => {
    await db.messages.bulkAdd([
      message('m1', 's1', '', 1, {
        role: 'assistant',
        clarification: {
          toolCallId: 'ask-1',
          question: '要不要改用向量库？',
          options: ['要', '不要'],
          allowCustomInput: true,
        },
      }),
    ]);

    const matches = await searchSessionContent(['向量库'], { sessionIds: ['s1'] });
    expect(matches.get('s1')!.messageCount).toBe(1);
    expect(matches.get('s1')!.label).toBe('提问');
    expect(matches.get('s1')!.snippet.text).toContain('向量库');
  });

  it('只读候选会话的消息', async () => {
    await db.messages.bulkAdd([
      message('m1', 's1', '向量在 s1', 1),
      message('m2', 's2', '向量在 s2', 2),
    ]);

    const matches = await searchSessionContent(['向量'], { sessionIds: ['s2'] });
    expect([...matches.keys()]).toEqual(['s2']);
  });

  it('已中止的查询直接返回空，不与新查询争抢结果', async () => {
    await db.messages.bulkAdd([message('m1', 's1', '向量', 1)]);

    const controller = new AbortController();
    controller.abort();

    const matches = await searchSessionContent(['向量'], {
      sessionIds: ['s1'],
      signal: controller.signal,
    });
    expect(matches.size).toBe(0);
  });

  it('空查询或空候选集合不打开事务，直接返回空', async () => {
    expect((await searchSessionContent([], { sessionIds: ['s1'] })).size).toBe(0);
    expect((await searchSessionContent(['向量'], { sessionIds: [] })).size).toBe(0);
  });
});
