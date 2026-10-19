import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { AccountBar } from './AccountBar';
import { SettingsPanel } from './SettingsDialog';
import { ThemeContext } from '../context/theme-context';

function withTheme(children: React.ReactNode, theme: 'light' | 'dark' | 'system' = 'system') {
  return createElement(
    ThemeContext.Provider,
    {
      value: {
        theme,
        resolvedTheme: theme === 'dark' ? 'dark' : 'light',
        setTheme: vi.fn(),
      },
    },
    children
  );
}

describe('AccountBar 侧边栏底部账号与设置入口', () => {
  it('展开状态下展示头像首字母、昵称与完整邮箱', () => {
    const html = renderToStaticMarkup(
      withTheme(
        createElement(AccountBar, {
          email: 'bennett@example.com',
          onLogout: vi.fn(),
          collapsed: false,
        })
      )
    );

    // 昵称为邮箱前缀 bennett，头像首字母为 B（邮箱本身只在弹出菜单里展示）
    expect(html).toContain('bennett');
    expect(html).toContain('B');
    expect(html).toContain('aria-label="用户账户与设置菜单"');
    // 取消了生硬的 border-t 分界线
    expect(html).not.toContain('border-t');
  });

  it('收缩状态（collapsed）下仅展示紧凑头像，不包含文本撑宽', () => {
    const html = renderToStaticMarkup(
      withTheme(
        createElement(AccountBar, {
          email: 'bennett@example.com',
          onLogout: vi.fn(),
          collapsed: true,
        })
      )
    );

    // 仍包含头像首字母与 title
    expect(html).toContain('B');
    expect(html).toContain('title="bennett (bennett@example.com)"');
    // 没有独立的文本块，避免撑大收缩宽度
    expect(html).not.toContain('leading-snug');
    expect(html).not.toContain('border-t');
  });

  it('email 为 null 时优雅回落为未登录与默认首字母', () => {
    const html = renderToStaticMarkup(
      withTheme(
        createElement(AccountBar, {
          email: null,
          onLogout: vi.fn(),
        })
      )
    );

    expect(html).toContain('未登录');
    expect(html).toContain('未');
  });
});

describe('SettingsPanel 设置面板内容', () => {
  it('渲染外观设置、账号信息与关于系统', () => {
    const html = renderToStaticMarkup(
      withTheme(
        createElement(SettingsPanel, {
          userEmail: 'alice@example.com',
        }),
        'dark'
      )
    );

    // 各个板块
    expect(html).toContain('当前账号');
    expect(html).toContain('外观与主题');
    expect(html).toContain('关于系统');

    // 用户信息
    expect(html).toContain('alice');
    expect(html).toContain('alice@example.com');

    // 三态主题选项
    expect(html).toContain('浅色');
    expect(html).toContain('深色');
    expect(html).toContain('跟随系统');

    // 系统版本信息
    expect(html).toContain('v0.1.0');
    expect(html).toContain('PydanticAI');
  });
});
