/**
 * 快捷键判定的行为锁。
 *
 * 这些条件在真实浏览器里靠手测很难覆盖（输入法、非 Latin 布局、Alt/Shift 组合），
 * 但写错就会变成「按下 ⌘K 打开了别的东西」或者「在中文输入法里误触发」。
 */

import { describe, expect, it } from 'vitest';

import {
  isApplePlatform,
  isSearchShortcut,
  isSettingsShortcut,
  searchShortcutLabel,
  settingsShortcutLabel,
  shortcutLabel,
  type ShortcutEventLike,
} from './keyboard-shortcuts';

const event = (overrides: Partial<ShortcutEventLike> = {}): ShortcutEventLike => ({
  key: 'k',
  code: 'KeyK',
  metaKey: false,
  ctrlKey: false,
  altKey: false,
  shiftKey: false,
  ...overrides,
});

describe('isSearchShortcut', () => {
  it('Meta+K 与 Ctrl+K 都认', () => {
    expect(isSearchShortcut(event({ metaKey: true }))).toBe(true);
    expect(isSearchShortcut(event({ ctrlKey: true }))).toBe(true);
  });

  it('没有修饰键、或叠加 Alt/Shift 都不算', () => {
    expect(isSearchShortcut(event())).toBe(false);
    expect(isSearchShortcut(event({ metaKey: true, shiftKey: true }))).toBe(false);
    expect(isSearchShortcut(event({ ctrlKey: true, altKey: true }))).toBe(false);
  });

  it('按物理键位判定：非 Latin 布局下 key 不是 k 也算', () => {
    expect(isSearchShortcut(event({ key: 'л', metaKey: true, code: 'KeyK' }))).toBe(true);
  });

  it('没有 code 时退回 key 判定（老浏览器 / 合成事件）', () => {
    expect(isSearchShortcut(event({ metaKey: true, code: undefined }))).toBe(true);
  });

  it('输入法组合中不抢键', () => {
    expect(isSearchShortcut(event({ metaKey: true, isComposing: true }))).toBe(false);
  });

  it('其它键不算', () => {
    expect(isSearchShortcut(event({ key: 'j', code: 'KeyJ', metaKey: true }))).toBe(false);
  });
});

describe('isSettingsShortcut', () => {
  it('⌘, / Ctrl+, 打开设置', () => {
    expect(isSettingsShortcut(event({ key: ',', code: 'Comma', metaKey: true }))).toBe(true);
    expect(isSettingsShortcut(event({ key: ',', code: 'Comma', ctrlKey: true }))).toBe(true);
  });

  it('物理键位优先于字符', () => {
    expect(isSettingsShortcut(event({ key: 'б', code: 'Comma', metaKey: true }))).toBe(true);
  });

  it('其它组合不算', () => {
    expect(isSettingsShortcut(event({ key: ',', code: 'Comma', metaKey: true, shiftKey: true }))).toBe(
      false,
    );
    expect(isSettingsShortcut(event({ metaKey: true }))).toBe(false);
  });
});

describe('提示文案', () => {
  it('Apple 平台用 ⌘，其它平台用 Ctrl+', () => {
    expect(searchShortcutLabel({ apple: true })).toBe('⌘K');
    expect(searchShortcutLabel({ apple: false })).toBe('Ctrl+K');
    expect(settingsShortcutLabel({ apple: true })).toBe('⌘,');
    expect(settingsShortcutLabel({ apple: false })).toBe('Ctrl+,');
  });

  it('单字母大写，非字母保持原样', () => {
    expect(shortcutLabel('k', { apple: true })).toBe('⌘K');
    expect(shortcutLabel(',', { apple: false })).toBe('Ctrl+,');
  });

  it('按 UA 识别 Apple 平台', () => {
    expect(isApplePlatform('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)')).toBe(true);
    expect(isApplePlatform('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)')).toBe(true);
    expect(isApplePlatform('Mozilla/5.0 (Windows NT 10.0; Win64; x64)')).toBe(false);
    expect(isApplePlatform('')).toBe(false);
  });
});
