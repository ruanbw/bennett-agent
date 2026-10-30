import { afterEach, describe, expect, it, vi } from 'vitest';

import { CONNECT_TIMEOUT_MESSAGE, apiFetch } from './api';

/**
 * `apiFetch` 的「等响应头」超时。
 *
 * 这里测的是旧实现的两个缺口：拿不到响应头时没有任何上界（请求发出去就永久挂着），
 * 以及调用方自带 `signal` 时 `timeoutMs` 被整条丢掉。两者叠加的现场是：反代建连
 * 挂住，用户对着「生成中」干等，而前端连一条中止路径都没有。
 */

/** 永不返回的请求：signal 一旦中止就以它的 reason 拒绝（真实 fetch 就是这么做的）。 */
function hangingFetch() {
  const fetchMock = vi.fn(
    (_url: string, init: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        const signal = init.signal;
        if (!signal) return;
        if (signal.aborted) {
          reject(signal.reason);
          return;
        }
        signal.addEventListener('abort', () => reject(signal.reason), { once: true });
      })
  );
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

/** 立刻返回响应头；把交给 fetch 的 signal 记下来，供「联动是否还在」的断言用。 */
function resolvedFetch(): { signal: () => AbortSignal | null | undefined } {
  let forwarded: AbortSignal | null | undefined;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url: string, init: RequestInit) => {
      forwarded = init.signal;
      return { ok: true, status: 200, headers: { get: () => null } } as unknown as Response;
    })
  );
  return { signal: () => forwarded };
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('等响应头的超时', () => {
  it('到点以 TimeoutError 中止，说明文字能区分「连不上」与「等太久」', async () => {
    vi.useFakeTimers();
    hangingFetch();

    const outcome = apiFetch('/api/chat', { timeoutMs: 30_000 }).then(
      () => null,
      (err: unknown) => err
    );

    // 没到点不能提前中止：30s 这个上界要留给慢网络下的正常请求
    await vi.advanceTimersByTimeAsync(29_999);
    let settled = false;
    void outcome.then(() => {
      settled = true;
    });
    await Promise.resolve();
    expect(settled).toBe(false);

    await vi.advanceTimersByTimeAsync(2);
    const err = await outcome;

    expect(err).toBeInstanceOf(DOMException);
    // 名字是 TimeoutError 而不是默认的 AbortError：后者在对话链路里等于「用户点了
    // 停止」，超时被记成主动停止就没人知道服务器没响应了
    expect((err as DOMException).name).toBe('TimeoutError');
    expect((err as Error).message).toBe(CONNECT_TIMEOUT_MESSAGE);
  });

  it('响应头一到就停表，不会在响应体还在读的时候把请求掐掉', async () => {
    vi.useFakeTimers();
    const fetchMock = resolvedFetch();

    await apiFetch('/api/models', { timeoutMs: 1_000 });

    expect(vi.getTimerCount()).toBe(0);

    // 超过原定超时很久，交出去的 signal 仍不该被中止：响应体是调用方在读，
    // 它不是「等响应头」的一部分
    await vi.advanceTimersByTimeAsync(10_000);
    expect(fetchMock.signal()?.aborted).toBe(false);
  });

  it('调用方 signal 与超时同时给时，两者都生效（旧实现把 timeoutMs 丢了）', async () => {
    vi.useFakeTimers();
    const caller = new AbortController();
    hangingFetch();

    const outcome = apiFetch('/api/chat', { signal: caller.signal, timeoutMs: 30_000 }).then(
      () => null,
      (err: unknown) => err
    );

    await vi.advanceTimersByTimeAsync(30_001);
    expect((await outcome) as DOMException).toMatchObject({ name: 'TimeoutError' });
  });

  it('调用方中途中止仍是 AbortError，不会变成超时', async () => {
    const caller = new AbortController();
    hangingFetch();

    const outcome = apiFetch('/api/chat', { signal: caller.signal, timeoutMs: 30_000 }).then(
      () => null,
      (err: unknown) => err
    );

    caller.abort();
    expect((await outcome) as DOMException).toMatchObject({ name: 'AbortError' });
  });

  it('响应头到手后调用方 signal 仍然联动：点停止能打断正在读的响应体', async () => {
    // fetch 的 signal 不只管建连，还管响应体：拿到响应头就把它摘掉的话，用户点停止
    // 时 body 不会 reject，reader.read() 会永远挂着，后端继续生成、继续计费。
    const caller = new AbortController();
    const fetchMock = resolvedFetch();

    await apiFetch('/api/chat', { signal: caller.signal, timeoutMs: 30_000 });
    caller.abort();

    expect(fetchMock.signal()?.aborted).toBe(true);
    expect(fetchMock.signal()?.reason.name).toBe('AbortError');
  });

  it('连不上（fetch 自己抛 TypeError）不会被当成超时', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('Failed to fetch');
      })
    );

    await expect(apiFetch('/api/models', { timeoutMs: 1_000 })).rejects.toBeInstanceOf(TypeError);
  });

  it('没有 timeoutMs 时把调用方 signal 原样交给 fetch，也不起计时器', async () => {
    vi.useFakeTimers();
    const caller = new AbortController();
    const fetchMock = resolvedFetch();

    await apiFetch('/api/models', { signal: caller.signal });

    expect(fetchMock.signal()).toBe(caller.signal);
    expect(vi.getTimerCount()).toBe(0);
  });
});
