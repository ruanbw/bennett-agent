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
      json: async () => ({
        web_search: true,
        sandbox: true,
        tools: ['ask_user_clarification', 'tavily_search', 'sandbox_exec'],
      }),
    });

    await expect(fetchCapabilities()).resolves.toEqual({
      web_search: true,
      sandbox: true,
      tools: ['ask_user_clarification', 'tavily_search', 'sandbox_exec'],
      mcp_servers: [],
      mcp_error: null,
    });
    expect(calls).toEqual(['/api/capabilities']);
  });

  it('非 2xx 抛错，由 hook 统一降级', async () => {
    stubFetch({ ok: false, status: 503 });

    await expect(fetchCapabilities()).rejects.toThrow('HTTP 503');
  });

  it('字段异常时保守降级：布尔只认 true，工具列表过滤非字符串', async () => {
    stubFetch({
      json: async () => ({ web_search: 'yes', sandbox: 'yes', tools: [1, 'tavily_search', null] }),
    });

    await expect(fetchCapabilities()).resolves.toEqual({
      web_search: false,
      sandbox: false,
      tools: ['tavily_search'],
      mcp_servers: [],
      mcp_error: null,
    });
  });

  it('响应不是对象时同样不抛，返回保守值', async () => {
    stubFetch({ json: async () => null });

    await expect(fetchCapabilities()).resolves.toEqual({
      web_search: false,
      // 老服务端不返回 sandbox：必须退化成 false，而不是 undefined 透传出去
      sandbox: false,
      tools: [],
      mcp_servers: [],
      mcp_error: null,
    });
  });
});

describe('MCP 能力', () => {
  it('解析 server 与工具清单', async () => {
    stubFetch({
      json: async () => ({
        web_search: false,
        tools: ['filesystem__read_file'],
        mcp_servers: [{ name: 'filesystem', tools: ['read_file'] }],
        mcp_error: null,
      }),
    });

    const capabilities = await fetchCapabilities();

    expect(capabilities.mcp_servers).toEqual([{ name: 'filesystem', tools: ['read_file'] }]);
    expect(capabilities.mcp_error).toBeNull();
  });

  it('丢掉无名条目（name 是渲染键，也是工具名前缀）', async () => {
    stubFetch({
      json: async () => ({
        web_search: false,
        tools: [],
        mcp_servers: [{ tools: ['a'] }, { name: 'ok', tools: ['b'] }, 'nope'],
      }),
    });

    await expect(fetchCapabilities()).resolves.toMatchObject({
      mcp_servers: [{ name: 'ok', tools: ['b'] }],
    });
  });

  it('保留连接失败的原因：工具从列表消失不该变得无从解释', async () => {
    stubFetch({
      json: async () => ({
        web_search: false,
        tools: [],
        mcp_servers: [],
        mcp_error: 'broken: 连接失败',
      }),
    });

    await expect(fetchCapabilities()).resolves.toMatchObject({ mcp_error: 'broken: 连接失败' });
  });
});
