import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// 用 Vite 的 ?raw 直接拿 index.html 原文：不引入 node 类型，也不需要新依赖
import indexHtml from '../../index.html?raw';

import {
  THEME_COLORS,
  THEME_STORAGE_KEY,
  normalizeThemePreference,
  readThemePreference,
  themeColorFor,
  themePreferenceFromStorageEvent,
  writeThemePreference,
} from './theme-context';

/**
 * 主题引导的两半：TS 侧的白名单（theme-context.ts）与 index.html 的首屏内联脚本。
 *
 * 内联脚本不能 import TS，所以两者是**两份副本**——这组用例的作用就是钉住「两份副本
 * 行为一致」：同一份脏值/合法的存储内容，两边必须解析出同一个主题。任何一边单独改歪，
 * 这里都会红，避免出现「首屏按 A 渲染、挂载后按 B 渲染」的闪变。
 */

interface BootSandboxOptions {
  stored?: string | null;
  storageThrows?: boolean;
  prefersDark?: boolean;
  matchMediaThrows?: boolean;
}

interface BootResult {
  classList: Set<string>;
  colorScheme: string | undefined;
  /** 首屏脚本写进 `<meta name="theme-color">` 的值（移动端工具栏颜色）。 */
  themeColor: string | null;
}

/** 取出 index.html 里的首屏脚本，在一个只实现了脚本所需 API 的假 DOM 里执行。 */
function runBootScript(options: BootSandboxOptions = {}): BootResult {
  const script = /<script>([\s\S]*?)<\/script>/.exec(indexHtml)?.[1];
  if (!script) throw new Error('index.html 中找不到首屏主题脚本');

  const classList = new Set<string>();
  const style: { colorScheme?: string } = {};
  const windowStub = {
    localStorage: {
      getItem: (key: string) => {
        if (options.storageThrows) throw new Error('SecurityError');
        return key === THEME_STORAGE_KEY ? (options.stored ?? null) : null;
      },
    },
    matchMedia: () => {
      if (options.matchMediaThrows) throw new Error('matchMedia 不可用');
      return { matches: options.prefersDark ?? false };
    },
  };
  let themeColor: string | null = null;
  const documentStub = {
    documentElement: {
      classList: { add: (name: string) => classList.add(name) },
      style,
    },
    querySelector: (selector: string) =>
      selector === 'meta[name="theme-color"]'
        ? { setAttribute: (_name: string, value: string) => { themeColor = value; } }
        : null,
  };

  // 在假 window/document 下执行真实脚本（等价于浏览器里的首屏执行）
  const run = new Function('window', 'document', script);
  run(windowStub, documentStub);

  return { classList, colorScheme: style.colorScheme, themeColor };
}

beforeEach(() => {
  // 兜底分支会打 warn，测试输出里不需要这些噪音
  vi.spyOn(console, 'warn').mockImplementation(() => {});
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('normalizeThemePreference（TS 侧白名单）', () => {
  it('三个合法值原样返回', () => {
    expect(normalizeThemePreference('light')).toBe('light');
    expect(normalizeThemePreference('dark')).toBe('dark');
    expect(normalizeThemePreference('system')).toBe('system');
  });

  it('脏值一律回落 system，而不是被断言成合法主题', () => {
    for (const dirty of ['Dark', 'DARK', 'blue', '', 'true', 42, null, undefined, {}]) {
      expect(normalizeThemePreference(dirty)).toBe('system');
    }
  });
});

describe('index.html 首屏脚本', () => {
  it('读合法 dark 时加 dark class 并同步 colorScheme', () => {
    const result = runBootScript({ stored: 'dark' });
    expect(result.classList.has('dark')).toBe(true);
    expect(result.colorScheme).toBe('dark');
  });

  it('脏值按 system 处理：系统是暗色就进暗色（与 Provider 一致）', () => {
    const result = runBootScript({ stored: 'Dark', prefersDark: true });
    expect(normalizeThemePreference('Dark')).toBe('system');
    expect(result.classList.has('dark')).toBe(true);
    expect(result.colorScheme).toBe('dark');
  });

  it('脏值 + 系统浅色：不加 dark，也不留下半套状态', () => {
    const result = runBootScript({ stored: 'Dark', prefersDark: false });
    expect(result.classList.has('dark')).toBe(false);
    expect(result.colorScheme).toBe('light');
  });

  it('localStorage 抛 SecurityError 时不冒泡，按 system 继续', () => {
    const result = runBootScript({ storageThrows: true, prefersDark: true });
    expect(result.classList.has('dark')).toBe(true);
    expect(result.colorScheme).toBe('dark');
  });

  it('matchMedia 抛异常时不冒泡，显式 dark 仍然生效', () => {
    const result = runBootScript({ stored: 'dark', matchMediaThrows: true });
    expect(result.classList.has('dark')).toBe(true);
    expect(result.colorScheme).toBe('dark');
  });

  it('storage 与 matchMedia 同时不可用也不抛异常', () => {
    const result = runBootScript({ storageThrows: true, matchMediaThrows: true });
    expect(result.classList.has('dark')).toBe(false);
    expect(result.colorScheme).toBe('light');
  });
});

describe('readThemePreference / writeThemePreference', () => {
  it('没有 window（SSR）时回落 system', () => {
    expect(readThemePreference()).toBe('system');
  });

  it('localStorage 访问本身抛异常（Safari 阻止 Cookie / sandbox iframe）时回落 system', () => {
    vi.stubGlobal('window', {
      get localStorage(): never {
        throw new Error('SecurityError');
      },
    });
    expect(readThemePreference()).toBe('system');
  });

  it('getItem 抛异常时回落 system', () => {
    vi.stubGlobal('window', {
      localStorage: {
        getItem: () => {
          throw new Error('SecurityError');
        },
        setItem: () => {},
      },
    });
    expect(readThemePreference()).toBe('system');
  });

  it('读到脏值回落 system，读到合法值原样返回', () => {
    vi.stubGlobal('window', {
      localStorage: { getItem: () => 'Dark', setItem: () => {} },
    });
    expect(readThemePreference()).toBe('system');

    vi.stubGlobal('window', {
      localStorage: { getItem: () => 'dark', setItem: () => {} },
    });
    expect(readThemePreference()).toBe('dark');
  });

  it('写失败（setItem 抛异常）不会把异常抛给调用方', () => {
    vi.stubGlobal('window', {
      localStorage: {
        getItem: () => null,
        setItem: () => {
          throw new Error('QuotaExceededError');
        },
      },
    });
    expect(() => writeThemePreference('dark')).not.toThrow();
  });
});

describe('themePreferenceFromStorageEvent（多标签同步）', () => {
  it('本键变化 → 采用新值（脏值同样回落 system）', () => {
    expect(themePreferenceFromStorageEvent({ key: THEME_STORAGE_KEY, newValue: 'light' })).toBe('light');
    expect(themePreferenceFromStorageEvent({ key: THEME_STORAGE_KEY, newValue: 'Dark' })).toBe('system');
  });

  it('clear()（key 为 null）→ 回落 system', () => {
    expect(themePreferenceFromStorageEvent({ key: null, newValue: null })).toBe('system');
  });

  it('其他键的事件 → 忽略', () => {
    expect(themePreferenceFromStorageEvent({ key: 'other_key', newValue: 'dark' })).toBeNull();
  });
});


describe('移动端工具栏颜色（theme-color）', () => {
  it('首屏脚本与 TS 侧给出的颜色一致（两份副本必须同源）', () => {
    for (const [stored, prefersDark] of [
      ['light', false],
      ['dark', true],
      ['system', true],
      ['system', false],
      ['Dark', true],
    ] as const) {
      const result = runBootScript({ stored, prefersDark });
      const expected = themeColorFor(result.classList.has('dark') ? 'dark' : 'light');

      expect(result.themeColor).toBe(expected);
    }
  });

  it('深色用 #09090b、浅色用 #ffffff（与 index.css 的 --background 一致）', () => {
    expect(THEME_COLORS.dark).toBe('#09090b');
    expect(THEME_COLORS.light).toBe('#ffffff');
  });

  it('index.html 里声明了这个 meta，否则脚本找不到它、移动端工具栏不会跟随', () => {
    expect(indexHtml).toContain('<meta name="theme-color"');
  });
});
