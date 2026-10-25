import { createContext, useContext } from 'react';

/**
 * 主题上下文。
 *
 * context 与读取 hook 同文件：此前拆成 `theme-context.js` + `use-theme.js`
 * （6 行纯转发），后者只是 Middle Man，改一次接口要动两个文件（O-3）。
 *
 * 主题取值的白名单与存储读写也放在这里：它们不依赖 React，可以脱离渲染直接测
 * （vitest 跑 node 环境，没有 jsdom）。
 */

export type ThemePreference = 'light' | 'system' | 'dark';
export type ResolvedTheme = 'light' | 'dark';

/**
 * 移动端浏览器工具栏（Android 地址栏、iOS 状态栏）用的主题色。
 *
 * 取值必须与 `index.css` 的 `--background` 一致（浅色 `#ffffff`、深色 `#09090b`），
 * 否则会出现「页面是暗的、地址栏还是白的」这种半截效果——`color-scheme` 管不到它。
 * 与首屏内联脚本同样要保持一致（那边是第二份副本，见 index.html 注释）。
 */
export const THEME_COLORS: Record<ResolvedTheme, string> = {
  light: '#ffffff',
  dark: '#09090b',
};

/** 解析后的主题 → 浏览器工具栏颜色。 */
export function themeColorFor(resolved: ResolvedTheme): string {
  return THEME_COLORS[resolved];
}

/** 主题偏好的存储键。 */
export const THEME_STORAGE_KEY = 'theme_preference';

/**
 * 主题白名单。
 *
 * 注意：`index.html` 的首屏内联脚本里有一份**等价副本**（内联脚本不能 import TS 模块），
 * 两处的白名单与兜底规则必须完全一致：只接受 'light' | 'dark' | 'system'，其余（含
 * 'Dark' 这类脏值、null）一律当 'system'——否则会出现「首屏按 A 渲染、挂载后按 B 渲染」
 * 的闪白/闪黑。改这里（或改 index.html）时必须同步另一处。
 */
export function normalizeThemePreference(value: unknown): ThemePreference {
  return value === 'light' || value === 'dark' || value === 'system' ? value : 'system';
}

/** localStorage 的最小接口：只用到 getItem/setItem，方便单测注入假实现。 */
export interface ThemeStorage {
  getItem: (key: string) => string | null;
  setItem: (key: string, value: string) => void;
}

/**
 * 取 localStorage 本身。
 *
 * 连「读属性」都要包 try：Safari 阻止 Cookie、`sandbox` iframe（无 `allow-same-origin`）下
 * 访问 `window.localStorage` 会直接抛 SecurityError。此前 ThemeProvider 在 useState 初始化
 * 里裸读，异常会一路冒到 ErrorBoundary——整个应用进错误页，重试也没用。
 */
export function getThemeStorage(): ThemeStorage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage;
  } catch (err) {
    console.warn('localStorage 不可用，主题偏好不会被持久化:', err);
    return null;
  }
}

/** 读主题偏好：读不到、读到脏值、存储抛异常都回落 'system'（与 index.html 内联脚本一致）。 */
export function readThemePreference(storage: ThemeStorage | null = getThemeStorage()): ThemePreference {
  if (!storage) return 'system';
  try {
    return normalizeThemePreference(storage.getItem(THEME_STORAGE_KEY));
  } catch (err) {
    console.warn('读取主题偏好失败，回落 system:', err);
    return 'system';
  }
}

/** 写主题偏好：写失败只记日志——主题已经切换生效，不该把存储异常抛给调用方。 */
export function writeThemePreference(
  theme: ThemePreference,
  storage: ThemeStorage | null = getThemeStorage()
): void {
  if (!storage) return;
  try {
    storage.setItem(THEME_STORAGE_KEY, theme);
  } catch (err) {
    console.warn('保存主题偏好失败:', err);
  }
}

/**
 * `storage` 事件 → 需要的主题；与本键无关的事件返回 null（调用方据此忽略）。
 *
 * `event.key === null` 表示 `storage.clear()`：偏好被清掉，同样回落 'system'。
 */
export function themePreferenceFromStorageEvent(
  event: Pick<StorageEvent, 'key' | 'newValue'>
): ThemePreference | null {
  if (event.key !== null && event.key !== THEME_STORAGE_KEY) return null;
  return normalizeThemePreference(event.newValue);
}

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
