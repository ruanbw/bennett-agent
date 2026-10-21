import { useCallback, useEffect, useMemo, useState } from 'react';
import type { PropsWithChildren } from 'react';

import { useMediaQuery } from '../hooks/use-media-query';
import { ThemeContext } from './theme-context';
import {
  readThemePreference,
  themeColorFor,
  themePreferenceFromStorageEvent,
  writeThemePreference,
} from './theme-context';
import type { ThemePreference } from './theme-context';

export function ThemeProvider({ children }: PropsWithChildren) {
  // 读取自带 try/catch：存储不可用（Safari 阻止 Cookie、sandbox iframe）时回落 'system'，
  // 而不是让 SecurityError 从 useState 初始化里抛出去打挂整个应用
  const [theme, setThemeState] = useState<ThemePreference>(() => readThemePreference());

  // 与 index.html 里的首屏内联脚本同源：都读 prefers-color-scheme
  const systemIsDark = useMediaQuery('(prefers-color-scheme: dark)');

  const resolvedTheme = useMemo(
    () => (theme === 'system' ? (systemIsDark ? 'dark' : 'light') : theme),
    [theme, systemIsDark]
  );

  useEffect(() => {
    const root = document.documentElement;
    const isDark = resolvedTheme === 'dark';
    root.classList.toggle('dark', isDark);
    // colorScheme 必须跟着切：滚动条、表单控件等浏览器原生 UI 由它决定，只切 class 不够
    root.style.colorScheme = isDark ? 'dark' : 'light';
    // 移动端浏览器工具栏不吃 color-scheme，要显式改 theme-color，否则地址栏会一直是白的
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', themeColorFor(resolvedTheme));
  }, [resolvedTheme]);

  const setTheme = useCallback((newTheme: ThemePreference) => {
    setThemeState(newTheme);
    writeThemePreference(newTheme);
  }, []);

  // 多标签同步：storage 事件只在「其他标签页写同名键」时触发，正好用来跟随别人的切换
  useEffect(() => {
    const handleStorage = (event: StorageEvent) => {
      const nextTheme = themePreferenceFromStorageEvent(event);
      if (nextTheme !== null) setThemeState(nextTheme);
    };

    window.addEventListener('storage', handleStorage);
    return () => window.removeEventListener('storage', handleStorage);
  }, []);

  // memo 化 value：否则每次 provider 重渲染都会让所有消费者跟着重渲染
  const value = useMemo(() => ({ theme, resolvedTheme, setTheme }), [theme, resolvedTheme, setTheme]);

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}
