import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  UNKNOWN_KNOWLEDGE,
  describeDocumentUsage,
  describeKnowledgeStatus,
  fetchDocuments,
  fetchKnowledgeCapabilities,
  parseCapabilities,
  parseDocumentList,
  parseSearchHits,
  searchKnowledge,
} from './knowledge';

interface StubResponse {
  ok?: boolean;
  status?: number;
  json?: () => Promise<unknown>;
}

function stubFetch(response: StubResponse) {
  const calls: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      calls.push(url);
      return {
        ok: response.ok ?? true,
        status: response.status ?? 200,
        json: response.json ?? (async () => ({})),
      };
    }),
  );
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('fetchKnowledgeCapabilities', () => {
  it('解析可用状态与模型名', async () => {
    stubFetch({
      json: async () => ({ enabled: true, embedding_model: 'text-embedding-3-small', reason: null }),
    });

    await expect(fetchKnowledgeCapabilities()).resolves.toEqual({
      enabled: true,
      embeddingModel: 'text-embedding-3-small',
      reason: null,
    });
  });

  it('未启用时带出原因（用户要能知道是部署没配）', async () => {
    stubFetch({
      json: async () => ({ enabled: false, embedding_model: null, reason: '未配置 EMBEDDING_MODEL' }),
    });

    const capabilities = await fetchKnowledgeCapabilities();

    expect(capabilities.enabled).toBe(false);
    expect(capabilities.reason).toContain('EMBEDDING_MODEL');
  });

  it('请求失败时降级为不可用，而不是把错误抛给界面', async () => {
    // 知识库是增强能力：它的状态拉不到不该让设置面板整体渲染失败
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 503, json: async () => ({}) })));

    await expect(fetchKnowledgeCapabilities()).resolves.toEqual(UNKNOWN_KNOWLEDGE);
  });

  it('响应形状不对时同样降级', () => {
    expect(parseCapabilities(null)).toEqual(UNKNOWN_KNOWLEDGE);
    expect(parseCapabilities('nope')).toEqual(UNKNOWN_KNOWLEDGE);
    // 只认严格的 true：字符串 "true" 不算启用
    expect(parseCapabilities({ enabled: 'true' }).enabled).toBe(false);
  });
});

describe('fetchDocuments', () => {
  it('解析文档列表与额度', async () => {
    stubFetch({
      json: async () => ({
        documents: [
          {
            id: 'd1',
            title: '退款政策',
            source: 'refund.md',
            chunk_count: 3,
            embedding_model: 'text-embedding-3-small',
            dimensions: 1536,
            created_at: '2026-01-01T00:00:00Z',
          },
        ],
        max_documents: 200,
        total_chunks: 3,
      }),
    });

    const list = await fetchDocuments();

    expect(list.documents).toHaveLength(1);
    expect(list.documents[0]).toMatchObject({ id: 'd1', title: '退款政策', chunkCount: 3 });
    expect(list.maxDocuments).toBe(200);
    expect(list.totalChunks).toBe(3);
  });

  it('丢掉缺 id 或 title 的条目（它们是渲染键与展示主体）', () => {
    const list = parseDocumentList({
      documents: [{ id: 'd1', title: 'ok' }, { title: '无 id' }, { id: 'd2' }, 'nope'],
      max_documents: 10,
      total_chunks: 0,
    });

    expect(list.documents.map((item) => item.id)).toEqual(['d1']);
  });

  it('非 2xx 抛出带服务端文案的错误（用户要知道该拆成几篇）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: false,
        status: 400,
        json: async () => ({ detail: '文档 30,000 字符超过上限 200,000' }),
      })),
    );

    await expect(fetchDocuments()).rejects.toThrow('超过上限');
  });
});

describe('parseSearchHits', () => {
  it('解析两路分数', () => {
    const hits = parseSearchHits({
      hits: [
        {
          document_id: 'd1',
          document_title: '退款政策',
          heading: '条件',
          text: '商品需完好',
          score: 0.82,
          vector_score: 0.9,
          lexical_score: 0.4,
        },
      ],
    });

    expect(hits[0]).toMatchObject({ documentTitle: '退款政策', heading: '条件', score: 0.82 });
  });

  it('丢掉缺正文或标题的条目', () => {
    expect(parseSearchHits({ hits: [{ document_title: 'x' }, { text: 'y' }, 'nope'] })).toEqual([]);
  });

  it('空响应返回空数组', () => {
    expect(parseSearchHits(null)).toEqual([]);
    expect(parseSearchHits({})).toEqual([]);
  });
});

describe('searchKnowledge', () => {
  it('把查询与 top_k 发给服务端', async () => {
    const calls: Array<{ url: string; body?: string }> = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push({ url, body: init?.body as string | undefined });
        return { ok: true, status: 200, json: async () => ({ hits: [] }) };
      }),
    );

    await searchKnowledge('退款', 3);

    expect(calls[0].url).toBe('/api/knowledge/search');
    expect(JSON.parse(calls[0].body as string)).toEqual({ query: '退款', top_k: 3 });
  });
});

describe('文案', () => {
  it('文档规模：显示用量与上限，否则灰掉的上传按钮无从解释', () => {
    expect(
      describeDocumentUsage({ documents: [{ id: 'd' } as never], maxDocuments: 200, totalChunks: 5 }),
    ).toBe('1 / 200 篇 · 5 个片段');
  });

  it('没有上限时只报篇数，不显示 0 / 0', () => {
    expect(describeDocumentUsage({ documents: [], maxDocuments: 0, totalChunks: 0 })).toBe('0 篇');
  });

  it('列表未加载时显示破折号而不是 0 篇', () => {
    expect(describeDocumentUsage(null)).toBe('—');
  });

  it('可用时不返回解释文案', () => {
    expect(describeKnowledgeStatus({ enabled: true, embeddingModel: 'm', reason: null })).toBeNull();
  });

  it('不可用时优先用服务端给的原因', () => {
    expect(
      describeKnowledgeStatus({ enabled: false, embeddingModel: null, reason: '服务端说：没配 key' }),
    ).toBe('服务端说：没配 key');
  });

  it('服务端没给原因时兜底一句可操作的说明', () => {
    expect(describeKnowledgeStatus({ enabled: false, embeddingModel: null, reason: null })).toContain(
      'EMBEDDING_MODEL',
    );
  });
});
