import { afterEach, describe, expect, it, vi } from 'vitest';

import { fetchCapabilities } from './capabilities';

interface StubResponse {
  ok?: boolean;
  status?: number;
  json?: () => Promise<unknown>;
}

/** 记录请求并返回一个最小 Response 形状；apiFetch 只读 ok/status/json。 */
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

describe('fetchCapabilities', () => {
  it('解析服务端能力清单', async () => {
    const calls = stubFetch({
      json: async () => ({ web_search: true, tools: ['ask_user_clarification', 'tavily_search'] }),
    });

    await expect(fetchCapabilities()).resolves.toEqual({
      web_search: true,
      tools: ['ask_user_clarification', 'tavily_search'],
    });
    expect(calls).toEqual(['/api/capabilities']);
  });

  it('非 2xx 抛错，由 hook 统一降级', async () => {
    stubFetch({ ok: false, status: 503 });

    await expect(fetchCapabilities()).rejects.toThrow('HTTP 503');
  });

  it('字段异常时保守降级：布尔只认 true，工具列表过滤非字符串', async () => {
    stubFetch({
      json: async () => ({ web_search: 'yes', tools: [1, 'tavily_search', null] }),
    });

    await expect(fetchCapabilities()).resolves.toEqual({
      web_search: false,
      tools: ['tavily_search'],
    });
  });

  it('响应不是对象时同样不抛，返回保守值', async () => {
    stubFetch({ json: async () => null });

    await expect(fetchCapabilities()).resolves.toEqual({ web_search: false, tools: [] });
  });
});
