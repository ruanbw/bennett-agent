/**
 * 全局键盘快捷键：判定与展示文案。
 *
 * 为什么单独成模块：`keydown` 的判定条件（修饰键、物理键位、输入法组合、默认已阻止）
 * 全是容易写错且难在组件里回归的细节。这里只留纯函数，组件负责挂监听。
 *
 * ## 判定规则
 *
 * - **Meta 或 Ctrl 都认**：Mac 用 ⌘K、Windows/Linux 用 Ctrl+K，不按平台分叉判定，
 *   否则在 iPad 外接键盘、或远程桌面这类「UA 与真实按键不一致」的环境里会失灵。
 * - **不认 Alt / Shift**：⌘⇧K 是别的意思（将来可以留给别的命令），不能顺手吞掉。
 * - **优先看 `event.code`（物理键位）**：俄文/希腊文等非 Latin 布局下 `event.key` 不是
 *   `k`，但物理键仍是 KeyK；只在没有 code 时才退回 `key`（测试与老浏览器）。
 * - **输入法组合中不抢键**（`isComposing`）：与输入法的候选词操作冲突时让给输入法。
 *
 * ## 展示文案
 *
 * 判定不区分平台，但提示必须符合用户键盘：Mac 显示 `⌘K`，其余显示 `Ctrl+K`。
 */

/** `keydown` 里用得到的字段。用最小形状而不是 `KeyboardEvent`：node 环境中可直接构造。 */
export interface ShortcutEventLike {
  key: string;
  code?: string;
  metaKey: boolean;
  ctrlKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
  isComposing?: boolean;
}

/** `aria-keyshortcuts` 取值：多个组合用空格分隔。 */
export const SEARCH_SHORTCUT_KEYS = 'Meta+K Control+K';
export const SETTINGS_SHORTCUT_KEYS = 'Meta+, Control+,';

function matchesCommandKey(event: ShortcutEventLike, key: string, code: string): boolean {
  if (event.isComposing) return false;
  if (!event.metaKey && !event.ctrlKey) return false;
  if (event.altKey || event.shiftKey) return false;
  if (event.code === code) return true;
  return event.key.toLowerCase() === key;
}

/** ⌘K / Ctrl+K：打开会话搜索。 */
export function isSearchShortcut(event: ShortcutEventLike): boolean {
  return matchesCommandKey(event, 'k', 'KeyK');
}

/** ⌘, / Ctrl+,：打开设置（与 macOS 的偏好设置习惯一致）。 */
export function isSettingsShortcut(event: ShortcutEventLike): boolean {
  return matchesCommandKey(event, ',', 'Comma');
}

/** 是否 Apple 平台（决定提示里写 ⌘ 还是 Ctrl）。 */
export function isApplePlatform(userAgent?: string): boolean {
  const ua = userAgent ?? (typeof navigator === 'undefined' ? '' : navigator.userAgent);
  return /Mac|iPhone|iPad|iPod/i.test(ua);
}

/**
 * 快捷键提示文案。
 *
 * 单字符键大写（`⌘K` / `Ctrl+K`），非字母键保持原样（`⌘,` / `Ctrl+,`）。
 */
export function shortcutLabel(key: string, options: { apple?: boolean } = {}): string {
  const apple = options.apple ?? isApplePlatform();
  const label = key.length === 1 && /\p{L}/u.test(key) ? key.toUpperCase() : key;
  return apple ? `⌘${label}` : `Ctrl+${label}`;
}

export function searchShortcutLabel(options: { apple?: boolean } = {}): string {
  return shortcutLabel('k', options);
}

export function settingsShortcutLabel(options: { apple?: boolean } = {}): string {
  return shortcutLabel(',', options);
}
