import { afterEach, describe, expect, it, vi } from 'vitest';

import { fetchSandboxStatus, sendSandboxHeartbeat } from './sandbox';

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
