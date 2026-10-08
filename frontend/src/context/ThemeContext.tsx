import { useCallback, useEffect, useMemo, useState } from 'react';
import type { PropsWithChildren } from 'react';

import { useMediaQuery } from '../hooks/use-media-query';
import { ThemeContext } from './theme-context';
import type { ThemePreference } from './theme-context';

const THEME_STORAGE_KEY = 'theme_preference';

/**
 * localStorage 只能给出 `string | null`，而主题状态只能是三个字面量之一。用类型守卫
 * 而不是 `as ThemePreference`：键里可能是旧版本/手改的值，断言会把脏数据当成合法主题
 * 一路带到 `resolvedTheme` 里。
 */
function isThemePreference(value: unknown): value is ThemePreference {
  return value === 'light' || value === 'dark' || value === 'system';
}

export function ThemeProvider({ children }: PropsWithChildren) {
  const [theme, setThemeState] = useState<ThemePreference>(() => {
    if (typeof window === 'undefined') return 'system';
    const stored = localStorage.getItem(THEME_STORAGE_KEY);
    return isThemePreference(stored) ? stored : 'system';
  });

  // 与 index.html 里的首屏内联脚本同源：都读 prefers-color-scheme
  const systemIsDark = useMediaQuery('(prefers-color-scheme: dark)');

  const resolvedTheme = useMemo(
    () => (theme === 'system' ? (systemIsDark ? 'dark' : 'light') : theme),
    [theme, systemIsDark]
  );

  useEffect(() => {
    const root = document.documentElement;
    if (resolvedTheme === 'dark') {
      root.classList.add('dark');
      root.style.colorScheme = 'dark';
    } else {
      root.classList.remove('dark');
      root.style.colorScheme = 'light';
    }
  }, [resolvedTheme]);

  const setTheme = useCallback((newTheme: ThemePreference) => {
    setThemeState(newTheme);
    try {
      localStorage.setItem(THEME_STORAGE_KEY, newTheme);
    } catch (err) {
      console.error('保存主题失败:', err);
    }
  }, []);

  // memo 化 value：否则每次 provider 重渲染都会让所有消费者跟着重渲染
  const value = useMemo(() => ({ theme, resolvedTheme, setTheme }), [theme, resolvedTheme, setTheme]);

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}
