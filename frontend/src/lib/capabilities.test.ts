import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  UNKNOWN_CAPABILITIES,
  describeToolCount,
  describeWebSearchCapability,
  fetchCapabilities,
} from './capabilities';

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

describe('能力状态文案', () => {
  it('能力清单缺席时显示未知，而不是「未启用」', () => {
    expect(describeWebSearchCapability(null)).toBe('未知');
    expect(describeWebSearchCapability(undefined)).toBe('未知');
  });

  it('按 web_search 布尔值给出结论', () => {
    expect(describeWebSearchCapability({ web_search: true, tools: ['tavily_search'] })).toBe('已启用');
    expect(describeWebSearchCapability(UNKNOWN_CAPABILITIES)).toBe('未启用');
  });

  it('工具数量：有则报数，清单缺席或为空说未知（不说「0 个工具」）', () => {
    expect(describeToolCount({ web_search: false, tools: ['a', 'b'] })).toBe('2 个');
    expect(describeToolCount(null)).toBe('未知');
    expect(describeToolCount(UNKNOWN_CAPABILITIES)).toBe('未知');
  });
});
