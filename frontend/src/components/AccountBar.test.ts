import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { AccountBar } from './AccountBar';
import { AccountMenu } from './AccountMenu';

/**
 * base-ui 的菜单内容挂在 portal 里、且关闭时不渲染，所以 SSR 拿不到菜单项。
 * 这里只把「内容容器」替换成直接渲染 children，其余（Trigger/Item/Group…）保持真实——
 * 断言的是「哪些入口存在」，不是 popup 的定位行为（那个由 Playwright 覆盖）。
 */
vi.mock('./ui/dropdown-menu', async () => {
  const actual = await vi.importActual<typeof import('./ui/dropdown-menu')>('./ui/dropdown-menu');
  return {
    ...actual,
    DropdownMenuContent: ({ children }: { children?: React.ReactNode }) =>
      createElement('div', { 'data-slot': 'menu-content' }, children),
  };
});
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
          onOpenSettings: vi.fn(),
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
          onOpenSettings: vi.fn(),
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
          onOpenSettings: vi.fn(),
          onLogout: vi.fn(),
        })
      )
    );

    expect(html).toContain('未登录');
    expect(html).toContain('未');
  });
});

describe('菜单内容只有一份（AccountMenu）', () => {
  it('账号菜单渲染出设置、主题三态与登出（侧边栏与胶囊工具栏共用）', () => {
    const html = renderToStaticMarkup(
      withTheme(
        createElement(AccountMenu, {
          trigger: createElement('button', { type: 'button' }, '触发器'),
          email: 'bennett@example.com',
          onLogout: vi.fn(),
          onOpenSettings: vi.fn(),
        })
      )
    );

    // 收起侧边栏后这些入口不该消失：它们现在由 AccountMenu 统一提供
    expect(html).toContain('bennett@example.com');
    expect(html).toContain('设置');
    expect(html).toContain('浅色');
    expect(html).toContain('深色');
    expect(html).toContain('跟随系统');
    expect(html).toContain('退出登录');

    // 主题是单选菜单项，不只是「带勾的三个普通项」：读屏要能知道当前选中哪个
    expect(html.match(/role="menuitemradio"/g)).toHaveLength(3);
    expect(html.match(/aria-checked="true"/g)).toHaveLength(1);
    expect(html.match(/aria-checked="false"/g)).toHaveLength(2);
  });
});
