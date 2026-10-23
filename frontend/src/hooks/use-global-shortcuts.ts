/**
 * 全局快捷键的挂载点。
 *
 * 只做三件事：忽略已被别人处理的按键、判定、调用对应动作。判定逻辑在
 * `lib/keyboard-shortcuts.ts`（纯函数、有单测），这里不重复任何条件。
 */

import { useEffect } from 'react';

import {
  isSearchShortcut,
  isSettingsShortcut,
} from '../lib/keyboard-shortcuts';

export interface GlobalShortcutHandlers {
  onOpenSearch: () => void;
  onOpenSettings: () => void;
  /**
   * 整屏页面（设置页）打开时置 true：此时 ⌘K 不再可用。
   *
   * 页面没有 `role="dialog"`，所以下面那道「模态打开时不抢快捷键」的守卫拦不住它；
   * 搜索抽屉会在页面背后展开，用户既看不见也回不去。设置键**不**随之禁用——它被
   * 当作「再按一次返回」。
   */
  blockSearch?: boolean;
}

export function useGlobalShortcuts({
  onOpenSearch,
  onOpenSettings,
  blockSearch = false,
}: GlobalShortcutHandlers): void {
  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent): void => {
      // 弹层/菜单已经处理过的按键不能再触发全局动作（Esc 关闭菜单时会 preventDefault）
      if (event.defaultPrevented) return;

      // 模态框打开时不抢快捷键：用户的上下文在弹层里（设置 / 确认框），
      // 背后再打开搜索或再开一个设置页只会让人困惑。
      if (document.querySelector('[role="dialog"], [role="alertdialog"]')) return;

      if (!blockSearch && isSearchShortcut(event)) {
        event.preventDefault();
        onOpenSearch();
        return;
      }

      if (isSettingsShortcut(event)) {
        event.preventDefault();
        onOpenSettings();
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [onOpenSearch, onOpenSettings, blockSearch]);
}
