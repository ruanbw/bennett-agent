import { afterEach, describe, expect, it, vi } from 'vitest';

import { MOBILE_QUERY, mediaQueryStore } from './use-media-query';

/**
 * 媒体查询 store 的契约。
 *
 * 这正是 `useSyncExternalStore` 依赖的三件事，而修复的正是旧写法在这三件事上的缺口：
 * 旧实现把 matches 存进 state，只在 effect 里补订阅，窗口期内的变化读不到；现在
 * `getSnapshot` 每次都现读，React 订阅后会立刻重读，丢不了。
 */

function createMatchMediaStub(initialMatches: boolean) {
  const listeners = new Set<() => void>();
  let matches = initialMatches;

  const queryList = {
    get matches() {
      return matches;
    },
    addEventListener: vi.fn((_type: string, listener: () => void) => {
      listeners.add(listener);
    }),
    removeEventListener: vi.fn((_type: string, listener: () => void) => {
      listeners.delete(listener);
    }),
  };

  return {
    matchMedia: vi.fn(() => queryList),
    queryList,
    setMatches: (next: boolean) => {
      matches = next;
    },
    emitChange: () => listeners.forEach((listener) => listener()),
    listenerCount: () => listeners.size,
  };
}

const stubWindow = (stub: ReturnType<typeof createMatchMediaStub>) => {
  vi.stubGlobal('window', { matchMedia: stub.matchMedia });
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('mediaQueryStore', () => {
  it('没有 window（SSR / 单测环境）时快照固定为 false，订阅是空操作', () => {
    const store = mediaQueryStore(MOBILE_QUERY);
    expect(store.getSnapshot()).toBe(false);
    expect(store.getServerSnapshot()).toBe(false);
    expect(() => store.subscribe(() => {})()).not.toThrow();
  });

  it('缺少 matchMedia 时也不抛异常（按不匹配处理）', () => {
    vi.stubGlobal('window', {});
    const store = mediaQueryStore(MOBILE_QUERY);
    expect(store.getSnapshot()).toBe(false);
    expect(() => store.subscribe(() => {})()).not.toThrow();
  });

  it('getSnapshot 现读当前值，不缓存过期结果', () => {
    const stub = createMatchMediaStub(false);
    stubWindow(stub);
    const store = mediaQueryStore(MOBILE_QUERY);

    expect(store.getSnapshot()).toBe(false);
    // 模拟「首次渲染取值 → 订阅之前」这个窗口期里发生的断点变化
    stub.setMatches(true);
    expect(store.getSnapshot()).toBe(true);
    expect(stub.matchMedia).toHaveBeenCalledWith(MOBILE_QUERY);
  });

  it('订阅后 change 事件能通知 React，退订后不再通知', () => {
    const stub = createMatchMediaStub(false);
    stubWindow(stub);
    const store = mediaQueryStore(MOBILE_QUERY);
    const onStoreChange = vi.fn();

    const unsubscribe = store.subscribe(onStoreChange);
    expect(stub.listenerCount()).toBe(1);
    expect(stub.queryList.addEventListener).toHaveBeenCalledWith('change', onStoreChange);

    stub.emitChange();
    expect(onStoreChange).toHaveBeenCalledTimes(1);

    unsubscribe();
    expect(stub.listenerCount()).toBe(0);
    expect(stub.queryList.removeEventListener).toHaveBeenCalledWith('change', onStoreChange);
  });
});
