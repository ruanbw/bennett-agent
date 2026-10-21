import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { ThemeContext, type ThemePreference } from '../context/theme-context';
import { ThemeSwitcher } from './ThemeSwitcher';

function render(theme: ThemePreference) {
  return renderToStaticMarkup(
    createElement(
      ThemeContext.Provider,
      { value: { theme, resolvedTheme: theme === 'dark' ? 'dark' : 'light', setTheme: vi.fn() } },
      createElement(ThemeSwitcher),
    ),
  );
}

describe('ThemeSwitcher', () => {
  it('三种主题组成一个有名字的组，且只有当前项 pressed', () => {
    const html = render('dark');

    expect(html).toContain('role="group"');
    expect(html).toContain('aria-label="外观主题"');
    expect(html.match(/aria-pressed="true"/g)).toHaveLength(1);
    expect(html.match(/aria-pressed="false"/g)).toHaveLength(2);
    expect(html).toContain('aria-label="深色模式"');
  });

  it('system 偏好时 pressed 的是「系统」而不是解析后的浅/深色', () => {
    const html = render('system');

    expect(html).toMatch(/aria-label="系统模式"[^>]*aria-pressed="true"/);
  });
});
