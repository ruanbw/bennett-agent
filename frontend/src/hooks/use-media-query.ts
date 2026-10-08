import { useEffect, useState } from 'react';

/** 侧边栏在移动端是抽屉、在桌面端是常驻栏。 */
export const MOBILE_QUERY = '(max-width: 767px)';

/** SSR 或测试环境里没有 window。 */
const readMatch = (query: string): boolean =>
  typeof window === 'undefined' ? false : window.matchMedia(query).matches;

/**
 * 响应式媒体查询。
 *
 * O-7：此前用 `isMobileViewport()` 按需求值 `window.matchMedia(...).matches`，
 * 没有 change 监听，于是窗口跨过断点（或平板旋转）后状态保持陈旧——移动端抽屉
 * 仍开着、遮罩仍挂着。`ThemeContext` 里本来有一份正确写法，这里抽成可复用 hook。
 *
 * query 变化用「渲染期调整 state」处理（React 官方文档的 pattern），而不是在
 * effect 里同步 setState——后者会多跑一轮渲染，也是 `react/set-state-in-effect`
 * 想拦的写法。
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => readMatch(query));
  const [trackedQuery, setTrackedQuery] = useState(query);

  if (trackedQuery !== query) {
    setTrackedQuery(query);
    setMatches(readMatch(query));
  }

  useEffect(() => {
    if (typeof window === 'undefined') return undefined;

    const mediaQuery = window.matchMedia(query);
    const handleChange = (event: MediaQueryListEvent): void => setMatches(event.matches);

    mediaQuery.addEventListener('change', handleChange);
    return () => mediaQuery.removeEventListener('change', handleChange);
  }, [query]);

  return matches;
}

export function useIsMobileViewport(): boolean {
  return useMediaQuery(MOBILE_QUERY);
}
