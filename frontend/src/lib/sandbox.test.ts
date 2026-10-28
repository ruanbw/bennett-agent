import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  describeSandboxState,
  fetchSandboxArtifacts,
  fetchSandboxStatus,
  formatSandboxArtifactSize,
  sendSandboxHeartbeat,
} from './sandbox';

/**
 * 沙箱客户端的两条请求：
 *   - 心跳失败**不能**冒到界面上（它是后台续命，不是用户操作）；
 *   - 状态查询把 404 当成「没有机器」这一正常状态，其余失败照旧上抛。
 */

const SESSION_ID = 'sess_frontendtest0001';

interface StubResponse {
  ok?: boolean;
  status?: number;
  json?: () => Promise<unknown>;
}

interface StubCall {
  url: string;
  init: RequestInit | undefined;
}

/** 记录请求并返回一个最小 Response 形状；apiFetch 只读 ok/status/json。 */
function stubFetch(response: StubResponse = {}) {
  const calls: StubCall[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      return {
        ok: response.ok ?? true,
        status: response.status ?? 204,
        json: response.json ?? (async () => ({})),
      };
    }),
  );
  return calls;
}

/** 心跳的失败只留一行 debug 日志；用例不让它污染测试输出。 */
function silenceDebug() {
  return vi.spyOn(console, 'debug').mockImplementation(() => {});
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('sendSandboxHeartbeat', () => {
  it('POST /api/sandbox/heartbeat，带上 cookie 与 session_id', async () => {
    const calls = stubFetch({ status: 204 });

    await sendSandboxHeartbeat(SESSION_ID);

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe('/api/sandbox/heartbeat');
    expect(calls[0].init?.method).toBe('POST');
    // 跨域部署下不带 credentials 就拿不到登录态，续命会静默失效
    expect(calls[0].init?.credentials).toBe('include');
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({ session_id: SESSION_ID });
  });

  it('网络失败不抛，只留 debug 日志', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('Failed to fetch');
      }),
    );
    const debug = silenceDebug();

    await expect(sendSandboxHeartbeat(SESSION_ID)).resolves.toBeUndefined();
    expect(debug).toHaveBeenCalled();
  });

  it('非 2xx 也不抛：续命失败不该在界面上冒错误', async () => {
    stubFetch({ ok: false, status: 500 });
    const debug = silenceDebug();

    await expect(sendSandboxHeartbeat(SESSION_ID)).resolves.toBeUndefined();
    expect(debug).toHaveBeenCalled();
  });

  it('旧格式 id（含下划线）照常发：服务端 pattern 与同步接口一样允许它', async () => {
    const calls = stubFetch();

    await sendSandboxHeartbeat('sess_1729000000000_ab12cd34');

    expect(JSON.parse(String(calls[0].init?.body))).toEqual({
      session_id: 'sess_1729000000000_ab12cd34',
    });
  });

  it('空值或形状不合法的 sessionId 不发请求（服务端会 422，发了只是噪音）', async () => {
    const calls = stubFetch();

    await sendSandboxHeartbeat('');
    await sendSandboxHeartbeat('not-a-session');

    expect(calls).toHaveLength(0);
  });
});

describe('fetchSandboxStatus', () => {
  it('按 session_id 查询并解析租约快照', async () => {
    const calls = stubFetch({
      status: 200,
      json: async () => ({
        state: 'ready',
        expires_at: '2026-10-24T12:00:00Z',
        artifact_uri: 's3://artifacts/sbx_abc.tar',
      }),
    });

    await expect(fetchSandboxStatus(SESSION_ID)).resolves.toEqual({
      state: 'ready',
      expires_at: '2026-10-24T12:00:00Z',
      artifact_uri: 's3://artifacts/sbx_abc.tar',
    });
    expect(calls[0].url).toBe(`/api/sandbox/status?session_id=${SESSION_ID}`);
  });

  it('404 = 这个会话没有机器，是正常状态而不是错误', async () => {
    stubFetch({ ok: false, status: 404 });

    await expect(fetchSandboxStatus(SESSION_ID)).resolves.toBeNull();
  });

  it('其余非 2xx 照旧上抛，由调用方决定怎么降级', async () => {
    stubFetch({ ok: false, status: 502 });

    await expect(fetchSandboxStatus(SESSION_ID)).rejects.toThrow('HTTP 502');
  });

  it('缺 state 时按「没有可用状态」处理，不把 undefined 透传给界面', async () => {
    stubFetch({ status: 200, json: async () => ({ expires_at: null }) });

    await expect(fetchSandboxStatus(SESSION_ID)).resolves.toBeNull();
  });

  it('可选字段缺失或类型不对时退化成 null，state 原样保留', async () => {
    stubFetch({ status: 200, json: async () => ({ state: 'provisioning', artifact_uri: 42 }) });

    await expect(fetchSandboxStatus(SESSION_ID)).resolves.toEqual({
      state: 'provisioning',
      expires_at: null,
      artifact_uri: null,
    });
  });

  it('未知状态原样透出：真实存在的机器不能被显示成「从未创建」', async () => {
    stubFetch({ status: 200, json: async () => ({ state: 'migrating' }) });

    await expect(fetchSandboxStatus(SESSION_ID)).resolves.toMatchObject({ state: 'migrating' });
  });

  it('空值或形状不合法的 sessionId 不发请求，直接按「查不到」收场', async () => {
    const calls = stubFetch();

    await expect(fetchSandboxStatus('')).resolves.toBeNull();
    await expect(fetchSandboxStatus('not-a-session')).resolves.toBeNull();
    expect(calls).toHaveLength(0);
  });
});

describe('fetchSandboxArtifacts', () => {
  it('按 session_id 查询并解析产物列表（含预签名 URL 与时效）', async () => {
    const calls = stubFetch({
      status: 200,
      json: async () => ({
        session_id: SESSION_ID,
        lease_id: 'sbx_abc',
        state: 'destroyed',
        artifacts: [
          {
            name: 'workspace.tar.gz',
            size_bytes: 12_345,
            download_url: 'https://storage.example/sbx_abc/workspace.tar.gz?sig=x',
            expires_at: '2026-10-28T10:30:00+00:00',
          },
        ],
      }),
    });

    await expect(fetchSandboxArtifacts(SESSION_ID)).resolves.toEqual({
      state: 'destroyed',
      artifacts: [
        {
          name: 'workspace.tar.gz',
          size_bytes: 12_345,
          download_url: 'https://storage.example/sbx_abc/workspace.tar.gz?sig=x',
          expires_at: '2026-10-28T10:30:00+00:00',
        },
      ],
    });
    expect(calls[0].url).toBe(`/api/sandbox/artifacts?session_id=${SESSION_ID}`);
  });

  it('没有产物时是空列表，不是 null（调用方不必为两种「空」写两套分支）', async () => {
    stubFetch({ status: 200, json: async () => ({ state: 'ready', artifacts: [] }) });

    await expect(fetchSandboxArtifacts(SESSION_ID)).resolves.toEqual({
      state: 'ready',
      artifacts: [],
    });
  });

  it('404 = 这个会话没有机器，返回空列表而不是抛错', async () => {
    stubFetch({ ok: false, status: 404 });

    await expect(fetchSandboxArtifacts(SESSION_ID)).resolves.toEqual({
      state: null,
      artifacts: [],
    });
  });

  it('其余非 2xx 照旧上抛，由调用方决定怎么降级', async () => {
    stubFetch({ ok: false, status: 502 });

    await expect(fetchSandboxArtifacts(SESSION_ID)).rejects.toThrow('HTTP 502');
  });

  it('缺 download_url 的条目丢掉：点了什么也不会发生的条目不显示', async () => {
    stubFetch({
      status: 200,
      json: async () => ({
        state: 'destroyed',
        artifacts: [
          { name: 'no-url.tar', size_bytes: 1 },
          { name: 'ok.tar', size_bytes: 2, download_url: 'https://x/ok', expires_at: 'e' },
          { download_url: 'https://x/nameless' },
          'nope',
          null,
        ],
      }),
    });

    const result = await fetchSandboxArtifacts(SESSION_ID);

    expect(result.artifacts.map((a) => a.name)).toEqual(['ok.tar']);
  });

  it('坏条目只丢自己，不连累同一批里的好条目', async () => {
    stubFetch({
      status: 200,
      json: async () => ({
        state: 'ready',
        artifacts: [
          { name: 'a.tar', size_bytes: 'big', download_url: 'https://x/a', expires_at: 'e1' },
          { name: 'b.tar', size_bytes: 2048, download_url: 'https://x/b', expires_at: 'e2' },
        ],
      }),
    });

    const result = await fetchSandboxArtifacts(SESSION_ID);

    // 大小类型不对退化成 0，条目本身留着（有可点的地址）
    expect(result.artifacts).toEqual([
      { name: 'a.tar', size_bytes: 0, download_url: 'https://x/a', expires_at: 'e1' },
      { name: 'b.tar', size_bytes: 2048, download_url: 'https://x/b', expires_at: 'e2' },
    ]);
  });

  it('响应不是对象或 artifacts 不是数组时不抛，返回空列表', async () => {
    stubFetch({ status: 200, json: async () => null });
    await expect(fetchSandboxArtifacts(SESSION_ID)).resolves.toEqual({
      state: null,
      artifacts: [],
    });

    stubFetch({ status: 200, json: async () => ({ state: 'ready', artifacts: 'nope' }) });
    await expect(fetchSandboxArtifacts(SESSION_ID)).resolves.toEqual({
      state: 'ready',
      artifacts: [],
    });
  });

  it('空值或形状不合法的 sessionId 不发请求', async () => {
    const calls = stubFetch();

    await expect(fetchSandboxArtifacts('')).resolves.toEqual({ state: null, artifacts: [] });
    await expect(fetchSandboxArtifacts('not-a-session')).resolves.toEqual({
      state: null,
      artifacts: [],
    });
    expect(calls).toHaveLength(0);
  });
});

describe('describeSandboxState', () => {
  it('七个已知状态都有中文文案', () => {
    const known: Record<string, string> = {
      provisioning: '正在启动',
      ready: '已就绪',
      paused: '已暂停',
      destroying: '正在回收',
      destroyed: '已回收',
      failed: '启动失败',
      destroy_failed: '回收失败',
    };

    for (const [state, label] of Object.entries(known)) {
      expect(describeSandboxState(state)).toBe(label);
    }
  });

  it('没有状态时是「未知」，不是「未创建」', () => {
    expect(describeSandboxState(null)).toBe('未知');
    expect(describeSandboxState(undefined)).toBe('未知');
    expect(describeSandboxState('')).toBe('未知');
  });

  it('后端新增的状态如实带出原始值：不能显示成机器不存在', () => {
    expect(describeSandboxState('migrating')).toBe('状态未知（migrating）');
    expect(describeSandboxState('migrating')).toContain('migrating');
  });
});

describe('formatSandboxArtifactSize', () => {
  it('0 字节说「空文件」，不说 0 B（那个读数像没算出来）', () => {
    expect(formatSandboxArtifactSize(0)).toBe('空文件');
  });

  it('小于 1KB 用字节', () => {
    expect(formatSandboxArtifactSize(1)).toBe('1 B');
    expect(formatSandboxArtifactSize(1023)).toBe('1023 B');
  });

  it('按 1024 进制逐级换算并保留一位小数', () => {
    expect(formatSandboxArtifactSize(1024)).toBe('1 KB');
    expect(formatSandboxArtifactSize(1536)).toBe('1.5 KB');
    expect(formatSandboxArtifactSize(1024 * 1024)).toBe('1 MB');
    expect(formatSandboxArtifactSize(12_345_678)).toBe('11.8 MB');
    expect(formatSandboxArtifactSize(3 * 1024 ** 3)).toBe('3 GB');
    expect(formatSandboxArtifactSize(1024 ** 4)).toBe('1 TB');
  });

  it('超大值停在 TB，不再往 PB 编一个单位', () => {
    expect(formatSandboxArtifactSize(2048 * 1024 ** 4)).toBe('2048 TB');
  });

  it('非法值说「未知大小」，不渲染 NaN/负数', () => {
    expect(formatSandboxArtifactSize(Number.NaN)).toBe('未知大小');
    expect(formatSandboxArtifactSize(-1)).toBe('未知大小');
    expect(formatSandboxArtifactSize(Number.POSITIVE_INFINITY)).toBe('未知大小');
  });
});
