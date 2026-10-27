import { useMemo, useSyncExternalStore } from 'react';

/** SSR / 单测环境没有 window；个别内嵌 WebView 还缺 matchMedia。 */
function getMediaQueryList(query: string): MediaQueryList | null {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return null;
  return window.matchMedia(query);
}

/**
 * 媒体查询的外部 store（subscribe / getSnapshot / getServerSnapshot）。
 *
 * 抽成独立对象而不是写在 hook 里，是为了让「订阅 / 取值」契约可以脱离 React 渲染
 * 直接测（vitest 跑 node 环境，没有 jsdom）。
 */
export function mediaQueryStore(query: string) {
  return {
    subscribe: (onStoreChange: () => void): (() => void) => {
      const mediaQueryList = getMediaQueryList(query);
      if (!mediaQueryList) return () => {};

      mediaQueryList.addEventListener('change', onStoreChange);
      return () => mediaQueryList.removeEventListener('change', onStoreChange);
    },
    // 每次都现读、不缓存：useSyncExternalStore 在订阅后会立刻重读一次快照，
    // 值万一在「首次渲染 → 挂上监听」之间变了也能补回来
    getSnapshot: (): boolean => getMediaQueryList(query)?.matches ?? false,
    getServerSnapshot: (): boolean => false,
  };
}

/**
 * 响应式媒体查询。
 *
 * O-7：此前用 `isMobileViewport()` 按需求值 `window.matchMedia(...).matches`，
 * 没有 change 监听，于是窗口跨过断点（或平板旋转）后状态保持陈旧——移动端抽屉
 * 仍开着、遮罩仍挂着。`ThemeContext` 里本来有一份正确写法，这里抽成可复用 hook。
 *
 * 这里用 `useSyncExternalStore` 而不是 `useState` + `useEffect`：后者在「初始化取值」
 * 与「effect 里订阅」之间有一个窗口，窗口内的变化会永久丢失（提交期间刚好跨断点就会
 * 命中，移动端表现为抽屉状态错一档）。useSyncExternalStore 由 React 保证订阅后重读
 * 快照，不丢变化；query 变化时连 store 一起换掉，不需要渲染期改 state。
 */
export function useMediaQuery(query: string): boolean {
  const store = useMemo(() => mediaQueryStore(query), [query]);
  return useSyncExternalStore(store.subscribe, store.getSnapshot, store.getServerSnapshot);
}
