import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  UNKNOWN_CAPABILITIES,
  describeMcpCapability,
  describeMcpServers,
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
      json: async () => ({ web_search: 'yes', tools: [1, 'tavily_search', null] }),
    });

    await expect(fetchCapabilities()).resolves.toEqual({
      web_search: false,
      tools: ['tavily_search'],
      mcp_servers: [],
      mcp_error: null,
    });
  });

  it('响应不是对象时同样不抛，返回保守值', async () => {
    stubFetch({ json: async () => null });

    await expect(fetchCapabilities()).resolves.toEqual({
      web_search: false,
      tools: [],
      mcp_servers: [],
      mcp_error: null,
    });
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

  it('文案：没配 / 配了但不通 / 通了几个，三者必须能区分', () => {
    const base = { web_search: false, tools: [] };

    expect(describeMcpCapability({ ...base, mcp_servers: [], mcp_error: null })).toBe('未配置');
    expect(describeMcpCapability({ ...base, mcp_servers: [], mcp_error: 'x: 连接失败' })).toBe(
      '配置了但未能连接',
    );
    expect(
      describeMcpCapability({
        ...base,
        mcp_servers: [
          { name: 'fs', tools: ['read', 'write'] },
          { name: 'db', tools: ['query'] },
        ],
        mcp_error: null,
      }),
    ).toBe('2 个 server · 3 个工具');
  });

  it('server 名字清单用于悬浮提示', () => {
    expect(
      describeMcpServers({
        web_search: false,
        tools: [],
        mcp_servers: [
          { name: 'fs', tools: [] },
          { name: 'db', tools: [] },
        ],
      }),
    ).toBe('fs、db');
    expect(describeMcpServers(null)).toBe('');
  });
});
