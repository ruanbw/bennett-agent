/**
 * 后台管理系统的标准外壳：可折叠左侧菜单 + 顶栏 + 通栏内容区。
 *
 * ## 布局
 *
 * - 左侧菜单即路由（react-router 的 `NavLink`）：每一项对应一个 URL，选中态由
 *   `isActive` 驱动。md 以上支持**折叠成图标栏**（状态存 localStorage），折叠后
 *   悬停可见 title 提示。
 * - 顶栏属于内容区一侧：左边是折叠开关，右边是主题切换、当前账号与登出——
 *   账号级操作放顶栏是后台管理系统的惯例，菜单栏只管导航。
 * - 内容区通栏（100% 宽），页面自己决定内部布局（筛选表单 + 数据表格）。
 *
 * 响应式做一档收敛：md 以下侧边栏变成顶部横向菜单（不参与折叠），顶栏保留。
 */

import { useCallback, useState } from 'react';
import { NavLink, Outlet } from 'react-router';
import {
  Coins,
  Loader2,
  LogOut,
  PanelLeftClose,
  PanelLeftOpen,
  Server,
  ShieldCheck,
  Users,
  type LucideIcon,
} from 'lucide-react';

import { ThemeSwitcher } from '../components/ThemeSwitcher';
import { cn } from '@/lib/utils';

interface MenuItem {
  to: string;
  label: string;
  Icon: LucideIcon;
}

const MENU: MenuItem[] = [
  { to: '/providers', label: '模型提供商', Icon: Server },
  { to: '/prices', label: '模型价格', Icon: Coins },
  { to: '/users', label: '用户管理', Icon: Users },
];

const COLLAPSE_STORAGE_KEY = 'admin_sidebar_collapsed';

/** 读折叠偏好：存储不可用（Safari 阻止 Cookie 等）时回落「展开」。 */
function readCollapsed(): boolean {
  try {
    return window.localStorage.getItem(COLLAPSE_STORAGE_KEY) === '1';
  } catch {
    return false;
  }
}

function writeCollapsed(collapsed: boolean): void {
  try {
    window.localStorage.setItem(COLLAPSE_STORAGE_KEY, collapsed ? '1' : '0');
  } catch {
    // 主题折叠偏好写不进去只影响下次打开的初始状态，不值得打扰用户
  }
}

/** 菜单项的选中态：底色 + 文字反色，读屏用 aria-current（NavLink 自动带）。 */
function menuItemClass({ isActive }: { isActive: boolean }): string {
  return cn(
    'flex shrink-0 items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition-colors outline-none',
    'cursor-pointer focus-visible:ring-1 focus-visible:ring-ring',
    isActive
      ? 'bg-accent font-medium text-foreground'
      : 'text-muted-foreground hover:bg-accent/60 hover:text-foreground',
  );
}

export interface AdminLayoutProps {
  userEmail: string | null;
  onLogout: () => Promise<void>;
  /** 登出请求进行中：禁用登出按钮，避免重复提交。 */
  loggingOut: boolean;
}

export function AdminLayout({ userEmail, onLogout, loggingOut }: AdminLayoutProps) {
  const [collapsed, setCollapsed] = useState(readCollapsed);

  const toggleCollapsed = useCallback(() => {
    setCollapsed((prev) => {
      writeCollapsed(!prev);
      return !prev;
    });
  }, []);

  return (
    <div className="app-shell flex bg-background text-foreground font-sans antialiased">
      {/* 左侧菜单：md 以上是可折叠竖栏，md 以下折化为顶部横向条（在 header 之下渲染）。
          用 hidden md:flex + 移动端单独一条，比让一套标记同时服务两种形态可读。 */}
      <aside
        className={cn(
          'hidden shrink-0 flex-col border-r border-border/70 py-3 transition-[width] md:flex',
          collapsed ? 'w-14 items-center px-1.5' : 'w-56 flex-col px-3',
        )}
      >
        <div className={cn('flex h-9 items-center', collapsed ? 'justify-center' : 'gap-2 px-1')}>
          <ShieldCheck className="size-4 shrink-0 text-primary" aria-hidden="true" />
          {!collapsed && <h1 className="text-sm font-semibold tracking-tight">管理后台</h1>}
        </div>

        <nav aria-label="管理后台菜单" className="mt-3 flex w-full flex-1 flex-col gap-1">
          {MENU.map(({ to, label, Icon }) => (
            <NavLink
              key={to}
              to={to}
              title={collapsed ? label : undefined}
              className={({ isActive }) =>
                cn(menuItemClass({ isActive }), 'w-full', collapsed && 'justify-center px-0')
              }
            >
              <Icon className="size-4 shrink-0" aria-hidden="true" />
              {!collapsed && <span className="whitespace-nowrap">{label}</span>}
            </NavLink>
          ))}
        </nav>
      </aside>

      {/* 右侧：顶栏 + 通栏内容 */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-12 shrink-0 items-center gap-2 border-b border-border/70 bg-background/95 px-3 backdrop-blur md:px-4">
          <div className="flex items-center gap-2 md:hidden">
            <ShieldCheck className="size-4 shrink-0 text-primary" aria-hidden="true" />
            <span className="text-sm font-semibold tracking-tight">管理后台</span>
          </div>
          <button
            type="button"
            onClick={toggleCollapsed}
            aria-label={collapsed ? '展开菜单栏' : '收起菜单栏'}
            aria-expanded={!collapsed}
            title={collapsed ? '展开菜单栏' : '收起菜单栏'}
            className="hidden size-8 cursor-pointer items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-accent/60 hover:text-foreground outline-none focus-visible:ring-1 focus-visible:ring-ring md:inline-flex"
          >
            {collapsed ? (
              <PanelLeftOpen className="size-4" aria-hidden="true" />
            ) : (
              <PanelLeftClose className="size-4" aria-hidden="true" />
            )}
          </button>

          <div className="ml-auto flex items-center gap-2">
            <ThemeSwitcher />
            <span
              className="hidden min-w-0 max-w-48 truncate text-xs text-muted-foreground lg:inline"
              title={userEmail ?? undefined}
            >
              {userEmail ?? '未登录'}
            </span>
            <button
              type="button"
              onClick={() => void onLogout()}
              disabled={loggingOut}
              aria-label="登出"
              title="登出"
              className="flex cursor-pointer items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-accent/60 hover:text-foreground disabled:opacity-50 outline-none focus-visible:ring-1 focus-visible:ring-ring"
            >
              {loggingOut ? (
                <Loader2 className="size-4 animate-spin" aria-hidden="true" />
              ) : (
                <LogOut className="size-4" aria-hidden="true" />
              )}
              <span className="hidden md:inline">登出</span>
            </button>
          </div>
        </header>

        {/* 移动端横向菜单（md 以下替代左侧栏） */}
        <nav
          aria-label="管理后台菜单"
          className="flex gap-1 overflow-x-auto border-b border-border/70 px-3 py-2 md:hidden"
        >
          {MENU.map(({ to, label, Icon }) => (
            <NavLink key={to} to={to} className={menuItemClass}>
              <Icon className="size-4 shrink-0" aria-hidden="true" />
              <span className="whitespace-nowrap">{label}</span>
            </NavLink>
          ))}
        </nav>

        <main className="min-w-0 flex-1 overflow-y-auto">
          <div className="w-full px-4 py-5 md:px-6 md:py-6">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}
