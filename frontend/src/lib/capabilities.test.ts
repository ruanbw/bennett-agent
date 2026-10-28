import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  UNKNOWN_CAPABILITIES,
  describeMcpCapability,
  describeMcpError,
  describeMcpServers,
  describeSandboxCapability,
  describeServerTools,
  describeToolCount,
  describeToolList,
  describeWebSearchCapability,
  fetchCapabilities,
  summarizeMcpServers,
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

describe('能力状态文案', () => {
  it('能力清单缺席时显示未知，而不是「未启用」', () => {
    expect(describeWebSearchCapability(null)).toBe('未知');
    expect(describeWebSearchCapability(undefined)).toBe('未知');
  });

  it('按 web_search 布尔值给出结论', () => {
    expect(
      describeWebSearchCapability({ web_search: true, sandbox: false, tools: ['tavily_search'] }),
    ).toBe('已启用');
    expect(describeWebSearchCapability(UNKNOWN_CAPABILITIES)).toBe('未启用');
  });

  it('工具数量：有则报数，清单缺席或为空说未知（不说「0 个工具」）', () => {
    expect(describeToolCount({ web_search: false, sandbox: false, tools: ['a', 'b'] })).toBe('2 个');
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
    const base = { web_search: false, sandbox: false, tools: [] };

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
        sandbox: false,
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

describe('设置页「运行时能力」的明细辅助', () => {
  const base = {
    web_search: true,
    sandbox: true,
    tools: ['a', 'b'],
    mcp_servers: [] as { name: string; tools: string[] }[],
    mcp_error: null as string | null,
  };

  it('summarizeMcpServers 给出每个 server 的工具数与清单（数量单独看不出来）', () => {
    expect(
      summarizeMcpServers({
        ...base,
        mcp_servers: [
          { name: 'fs', tools: ['read', 'write'] },
          { name: 'db', tools: [] },
        ],
      })
    ).toEqual([
      { name: 'fs', toolCount: 2, tools: ['read', 'write'] },
      { name: 'db', toolCount: 0, tools: [] },
    ]);
    expect(summarizeMcpServers(null)).toEqual([]);
  });

  it('summarizeMcpServers 复制工具数组，不与源数据共享引用', () => {
    const source = { ...base, mcp_servers: [{ name: 'fs', tools: ['read'] }] };
    const summary = summarizeMcpServers(source);

    summary[0].tools.push('injected');

    expect(source.mcp_servers[0].tools).toEqual(['read']);
  });

  it('describeServerTools：没有工具时说「未提供工具」，不留空串', () => {
    expect(describeServerTools(['read', 'write'])).toBe('read、write');
    expect(describeServerTools([])).toBe('未提供工具');
  });

  it('describeToolList 列出名字，空清单说「暂无」', () => {
    expect(describeToolList(base)).toBe('a、b');
    expect(describeToolList({ ...base, tools: [] })).toBe('暂无已装配的工具');
    expect(describeToolList(null)).toBe('暂无已装配的工具');
  });

  it('describeMcpError 原样返回上游错误，不改写', () => {
    const raw = 'filesystem: spawn npx ENOENT';
    expect(describeMcpError({ ...base, mcp_error: raw })).toBe(raw);
  });

  it('describeMcpError：null / 缺失 = 没有错误；空串 = 有错误但没给原因', () => {
    expect(describeMcpError({ ...base, mcp_error: null })).toBe('');
    expect(describeMcpError(base)).toBe('');
    expect(describeMcpError({ ...base, mcp_error: undefined })).toBe('');
    // 空串是「配了但不通、但服务端没给原因」，不能和「没有错误」混为一谈
    expect(describeMcpError({ ...base, mcp_error: '' })).toBe('（服务端没有给出具体原因）');
    expect(describeMcpError(null)).toBe('');
  });

  it('describeSandboxCapability 与联网搜索同一套语义', () => {
    expect(describeSandboxCapability(base)).toBe('已启用');
    expect(describeSandboxCapability({ ...base, sandbox: false })).toBe('未启用');
    expect(describeSandboxCapability(null)).toBe('未知');
    expect(describeSandboxCapability(undefined)).toBe('未知');
  });
});
