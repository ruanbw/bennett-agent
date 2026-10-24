import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  send: vi.fn(async (_sessionId: string) => {}),
}));

/** 只替换网络那一步：间隔常量仍走真实实现，免得用例与实现各说各话。 */
vi.mock('../lib/sandbox', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/sandbox')>();
  return { ...actual, sendSandboxHeartbeat: mocks.send };
});

import { SANDBOX_HEARTBEAT_INTERVAL_MS } from '../lib/sandbox';
import { startSandboxHeartbeat } from './use-sandbox-heartbeat';

/**
 * 心跳调度契约（hook 只是把它挂到会话切换/卸载上）：
 *   - 可见时立刻发一次，之后每个周期一次；
 *   - 隐藏期间一次都不发；
 *   - 切回可见立刻补一次，不等满一个周期；
 *   - 停表后定时器与监听都要清掉。
 *
 * 之所以不渲染 hook：前端测试跑 node 环境、没有 React 渲染器也不引新依赖，而这几条
 * 都发生在 `startSandboxHeartbeat` 里（与 `use-media-query` 把 store 抽出来测同理）。
 */

const SESSION_ID = 'sess_frontendtest0001';

function stubDocument(initial: 'visible' | 'hidden') {
  const listeners = new Set<() => void>();
  const state: { visibilityState: DocumentVisibilityState } = { visibilityState: initial };

  vi.stubGlobal('document', {
    get visibilityState() {
      return state.visibilityState;
    },
    addEventListener: (type: string, listener: () => void) => {
      if (type === 'visibilitychange') listeners.add(listener);
    },
    removeEventListener: (type: string, listener: () => void) => {
      if (type === 'visibilitychange') listeners.delete(listener);
    },
  });

  return {
    setVisibility(next: DocumentVisibilityState) {
      state.visibilityState = next;
    },
    emitVisibilityChange() {
      for (const listener of [...listeners]) listener();
    },
    listenerCount: () => listeners.size,
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  mocks.send.mockClear();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('startSandboxHeartbeat', () => {
  it('可见时立刻发一次，之后每 30s 一次', () => {
    stubDocument('visible');

    startSandboxHeartbeat(SESSION_ID);

    expect(mocks.send).toHaveBeenCalledTimes(1);
    expect(mocks.send).toHaveBeenLastCalledWith(SESSION_ID);

    vi.advanceTimersByTime(SANDBOX_HEARTBEAT_INTERVAL_MS);
    expect(mocks.send).toHaveBeenCalledTimes(2);

    vi.advanceTimersByTime(SANDBOX_HEARTBEAT_INTERVAL_MS * 2);
    expect(mocks.send).toHaveBeenCalledTimes(4);
  });

  it('挂载时隐藏则一次都不发，隐藏期间也不累计', () => {
    stubDocument('hidden');

    startSandboxHeartbeat(SESSION_ID);
    vi.advanceTimersByTime(SANDBOX_HEARTBEAT_INTERVAL_MS * 3);

    expect(mocks.send).not.toHaveBeenCalled();
  });

  it('切到隐藏停表，切回可见立刻补一次并恢复周期', () => {
    const doc = stubDocument('visible');
    startSandboxHeartbeat(SESSION_ID);
    expect(mocks.send).toHaveBeenCalledTimes(1);

    doc.setVisibility('hidden');
    doc.emitVisibilityChange();
    vi.advanceTimersByTime(SANDBOX_HEARTBEAT_INTERVAL_MS * 2);
    expect(mocks.send).toHaveBeenCalledTimes(1);

    // 切回标签页不该干等一个周期才续命
    doc.setVisibility('visible');
    doc.emitVisibilityChange();
    expect(mocks.send).toHaveBeenCalledTimes(2);

    vi.advanceTimersByTime(SANDBOX_HEARTBEAT_INTERVAL_MS);
    expect(mocks.send).toHaveBeenCalledTimes(3);
  });

  it('停表后不再发，也不留 visibilitychange 监听', () => {
    const doc = stubDocument('visible');
    const stop = startSandboxHeartbeat(SESSION_ID);

    stop();
    vi.advanceTimersByTime(SANDBOX_HEARTBEAT_INTERVAL_MS * 3);

    expect(mocks.send).toHaveBeenCalledTimes(1);
    expect(doc.listenerCount()).toBe(0);
  });

  it('发不出去的 sessionId（空值 / 形状不合法）既不发请求也不起表', () => {
    stubDocument('visible');

    const stops = [startSandboxHeartbeat(''), startSandboxHeartbeat('not-a-session')];
    vi.advanceTimersByTime(SANDBOX_HEARTBEAT_INTERVAL_MS * 2);

    // 这个会话本来就没有机器可续，每 30s 发一次注定 422 的请求只会变成噪音
    expect(mocks.send).not.toHaveBeenCalled();
    for (const stop of stops) stop();
  });
});
