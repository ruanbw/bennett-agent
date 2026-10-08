import { createContext, useContext } from 'react';

/**
 * 主题上下文。
 *
 * context 与读取 hook 同文件：此前拆成 `theme-context.js` + `use-theme.js`
 * （6 行纯转发），后者只是 Middle Man，改一次接口要动两个文件（O-3）。
 */

export type ThemePreference = 'light' | 'system' | 'dark';
export type ResolvedTheme = 'light' | 'dark';

export interface ThemeContextValue {
  theme: ThemePreference;
  resolvedTheme: ResolvedTheme;
  setTheme: (theme: ThemePreference) => void;
}

export const ThemeContext = createContext<ThemeContextValue>({
  theme: 'system',
  resolvedTheme: 'light',
  setTheme: () => {},
});

export function useTheme(): ThemeContextValue {
  return useContext(ThemeContext);
}
