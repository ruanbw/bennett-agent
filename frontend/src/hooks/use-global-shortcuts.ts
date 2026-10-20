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
}

export function useGlobalShortcuts({ onOpenSearch, onOpenSettings }: GlobalShortcutHandlers): void {
  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent): void => {
      // 弹层/菜单已经处理过的按键不能再触发全局动作（Esc 关闭菜单时会 preventDefault）
      if (event.defaultPrevented) return;

      // 模态框打开时不抢快捷键：用户的上下文在弹层里（设置 / 确认框），
      // 背后再打开搜索或再开一个设置面板只会让人困惑。
      if (document.querySelector('[role="dialog"], [role="alertdialog"]')) return;

      if (isSearchShortcut(event)) {
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
  }, [onOpenSearch, onOpenSettings]);
}
