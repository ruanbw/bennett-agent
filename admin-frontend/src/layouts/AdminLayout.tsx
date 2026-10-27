/**
 * 后台外壳：经典「左侧菜单 + 顶栏 + 内容区」三段式。
 *
 * ## 布局
 *
 * - **左侧菜单**（`md` 以上常驻、可折叠成图标栏，折叠状态存 localStorage）：
 *   分组标题 + 图标 + 文字。菜单即路由（`NavLink`），刷新与直达都不丢状态。
 * - **顶栏**：移动端汉堡（打开抽屉菜单）→ 面包屑 → 右侧（主题、账号菜单）。
 *   面包屑从 `lib/nav` 的路径表推导，不手写，加页面只改一处。
 * - **内容区**：`PageShell` 自己决定标题与主操作；外壳只给内边距与滚动。
 *
 * ## 为什么账号菜单而不是顶栏直接放「登出」按钮
 *
 * 经典后台把账号相关的一律收进右上角头像菜单（账号信息、外观、登出），
 * 顶栏才放得下更多后续入口；也避免「登出」这个破坏性动作一直挂在显眼位置被误点。
 *
 * ## 移动端
 *
 * `md` 以下侧边栏变成抽屉（base-ui Drawer，自带焦点陷阱、Esc 关闭、滑动手势），
 * 而不是把菜单折成一行横向滚动的标签——后者在菜单变多时会藏起一半入口。
 */

import { useCallback, useState } from 'react';
import { NavLink, Outlet, useLocation } from 'react-router';
import { Drawer as DrawerPrimitive } from '@base-ui/react/drawer';
import {
  ChevronRight,
  Laptop,
  Loader2,
  LogOut,
  Menu,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  ShieldCheck,
  Sun,
} from 'lucide-react';

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { useTheme, type ThemePreference } from '@/context/theme-context';
import { NAV_ITEMS, breadcrumbsFor } from '@/lib/nav';
import { cn } from '@/lib/utils';

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
    // 写不进去只影响下次打开的初始状态，不值得打扰用户
  }
}

const THEME_OPTIONS: Array<{ value: ThemePreference; label: string; Icon: typeof Sun }> = [
  { value: 'light', label: '浅色', Icon: Sun },
  { value: 'system', label: '跟随系统', Icon: Laptop },
  { value: 'dark', label: '深色', Icon: Moon },
];

export interface AdminLayoutProps {
  userEmail: string | null;
  /** 当前账号的角色标签（顶栏菜单里展示，让人确认自己拿的是什么权限）。 */
  roleLabel: string;
  onLogout: () => Promise<void>;
  /** 登出请求进行中：禁用入口，避免重复提交。 */
  loggingOut: boolean;
}

export function AdminLayout({ userEmail, roleLabel, onLogout, loggingOut }: AdminLayoutProps) {
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const location = useLocation();
  const crumbs = breadcrumbsFor(location.pathname);

  const toggleCollapsed = useCallback(() => {
    setCollapsed((prev) => {
      writeCollapsed(!prev);
      return !prev;
    });
  }, []);

  return (
    <div className="app-shell flex bg-muted/30 text-foreground font-sans antialiased">
      <Sidebar collapsed={collapsed} />

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center gap-2 border-b border-border/70 bg-background px-3 md:px-5">
          {/* 移动端：抽屉菜单 */}
          <DrawerPrimitive.Root open={drawerOpen} onOpenChange={setDrawerOpen} swipeDirection="left">
            <DrawerPrimitive.Trigger
              aria-label="打开菜单"
              className="inline-flex size-8 cursor-pointer items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:ring-1 focus-visible:ring-ring focus-visible:outline-none md:hidden"
            >
              <Menu className="size-4" aria-hidden="true" />
            </DrawerPrimitive.Trigger>
            <DrawerPrimitive.Portal>
              <DrawerPrimitive.Backdrop className="fixed inset-0 z-50 bg-black/30 backdrop-blur-[2px] transition-opacity duration-200 data-ending-style:opacity-0 data-starting-style:opacity-0" />
              <DrawerPrimitive.Viewport className="fixed inset-0 z-50 flex items-stretch justify-start">
                <DrawerPrimitive.Popup className="flex h-full w-64 flex-col border-r border-border bg-card p-3 shadow-xl transition-transform duration-250 ease-out data-ending-style:-translate-x-full data-starting-style:-translate-x-full">
                  <DrawerPrimitive.Title className="flex items-center gap-2 px-1 pb-3 text-sm font-semibold">
                    <ShieldCheck className="size-4 text-primary" aria-hidden="true" />
                    Bennett 管理后台
                  </DrawerPrimitive.Title>
                  <nav aria-label="管理后台菜单" className="flex flex-1 flex-col gap-1 overflow-y-auto">
                    <MenuItems onNavigate={() => setDrawerOpen(false)} />
                  </nav>
                </DrawerPrimitive.Popup>
              </DrawerPrimitive.Viewport>
            </DrawerPrimitive.Portal>
          </DrawerPrimitive.Root>

          <button
            type="button"
            onClick={toggleCollapsed}
            aria-label={collapsed ? '展开菜单栏' : '收起菜单栏'}
            aria-expanded={!collapsed}
            title={collapsed ? '展开菜单栏' : '收起菜单栏'}
            className="hidden size-8 cursor-pointer items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:ring-1 focus-visible:ring-ring focus-visible:outline-none md:inline-flex"
          >
            {collapsed ? (
              <PanelLeftOpen className="size-4" aria-hidden="true" />
            ) : (
              <PanelLeftClose className="size-4" aria-hidden="true" />
            )}
          </button>

          <Breadcrumbs crumbs={crumbs} />

          <div className="ml-auto flex items-center gap-1">
            <AccountMenu
              userEmail={userEmail}
              roleLabel={roleLabel}
              onLogout={onLogout}
              loggingOut={loggingOut}
            />
          </div>
        </header>

        <main className="min-w-0 flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-[1600px] px-4 py-5 md:px-6 md:py-6">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}

/** 桌面侧边栏：品牌 + 分组菜单 + 底部账号信息。 */
function Sidebar({ collapsed }: { collapsed: boolean }) {
  const groups = Array.from(new Set(NAV_ITEMS.map((item) => item.group)));

  return (
    <aside
      className={cn(
        'hidden shrink-0 flex-col border-r border-border/70 bg-background transition-[width] duration-200 md:flex',
        collapsed ? 'w-14 items-center px-1.5 py-3' : 'w-56 px-3 py-3',
      )}
    >
      <div className={cn('flex h-9 items-center', collapsed ? 'justify-center' : 'gap-2 px-1')}>
        <ShieldCheck className="size-4 shrink-0 text-primary" aria-hidden="true" />
        {!collapsed && <h1 className="truncate text-sm font-semibold tracking-tight">Bennett 管理后台</h1>}
      </div>

      <nav aria-label="管理后台菜单" className="mt-4 flex w-full flex-1 flex-col gap-4 overflow-y-auto">
        {groups.map((group) => (
          <div key={group} className="flex flex-col gap-0.5">
            {!collapsed && (
              <div className="px-2 pb-1 text-[11px] font-medium tracking-wide text-muted-foreground">
                {group}
              </div>
            )}
            {NAV_ITEMS.filter((item) => item.group === group).map((item) => (
              <SidebarLink key={item.to} item={item} collapsed={collapsed} />
            ))}
          </div>
        ))}
      </nav>
    </aside>
  );
}

function SidebarLink({
  item,
  collapsed,
}: {
  item: (typeof NAV_ITEMS)[number];
  collapsed: boolean;
}) {
  const { to, label, Icon } = item;
  return (
    <NavLink
      to={to}
      title={collapsed ? label : undefined}
      className={({ isActive }) =>
        cn(
          'flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition-colors outline-none',
          'cursor-pointer focus-visible:ring-1 focus-visible:ring-ring',
          collapsed && 'justify-center px-0',
          isActive
            ? 'bg-primary/10 font-medium text-primary'
            : 'text-muted-foreground hover:bg-accent hover:text-foreground',
        )
      }
    >
      <Icon className="size-4 shrink-0" aria-hidden="true" />
      {!collapsed && <span className="truncate">{label}</span>}
    </NavLink>
  );
}

/** 移动端抽屉里的菜单（始终展开，带图标 + 文字）。 */
function MenuItems({ onNavigate }: { onNavigate: () => void }) {
  return (
    <>
      {NAV_ITEMS.map(({ to, label, Icon }) => (
        <NavLink
          key={to}
          to={to}
          onClick={onNavigate}
          className={({ isActive }) =>
            cn(
              'flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition-colors',
              isActive
                ? 'bg-primary/10 font-medium text-primary'
                : 'text-muted-foreground hover:bg-accent hover:text-foreground',
            )
          }
        >
          <Icon className="size-4 shrink-0" aria-hidden="true" />
          {label}
        </NavLink>
      ))}
    </>
  );
}

/** 面包屑：首页 / 当前页。最后一级不可点（就是当前页）。 */
function Breadcrumbs({ crumbs }: { crumbs: ReturnType<typeof breadcrumbsFor> }) {
  return (
    <nav aria-label="面包屑" className="min-w-0">
      <ol className="flex items-center gap-1 text-xs text-muted-foreground">
        {crumbs.map((crumb, index) => {
          const last = index === crumbs.length - 1;
          return (
            <li key={`${crumb.label}-${index}`} className="flex min-w-0 items-center gap-1">
              {index > 0 && <ChevronRight className="size-3 shrink-0 opacity-50" aria-hidden="true" />}
              {crumb.to && !last ? (
                <NavLink to={crumb.to} className="truncate rounded transition-colors hover:text-foreground">
                  {crumb.label}
                </NavLink>
              ) : (
                <span
                  className={cn('truncate', last && 'font-medium text-foreground')}
                  aria-current={last ? 'page' : undefined}
                >
                  {crumb.label}
                </span>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

/**
 * 右上角账号菜单：账号信息 + 外观 + 登出。
 *
 * 外观三档放进菜单而不是顶栏常驻一组按钮：顶栏留给导航与账号，
 * 主题这种「设一次就不动」的偏好不值得占一个常驻控件。
 */
function AccountMenu({
  userEmail,
  roleLabel,
  onLogout,
  loggingOut,
}: {
  userEmail: string | null;
  roleLabel: string;
  onLogout: () => Promise<void>;
  loggingOut: boolean;
}) {
  const { theme, setTheme } = useTheme();
  const initial = (userEmail ?? '?').slice(0, 1).toUpperCase();

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        aria-label="账号菜单"
        className="flex cursor-pointer items-center gap-2 rounded-lg py-1 pr-2 pl-1 transition-colors hover:bg-accent focus-visible:ring-1 focus-visible:ring-ring focus-visible:outline-none"
      >
        <span
          aria-hidden="true"
          className="flex size-7 shrink-0 items-center justify-center rounded-full bg-primary text-[11px] font-semibold text-primary-foreground"
        >
          {initial}
        </span>
        <span className="hidden max-w-40 min-w-0 flex-col items-start leading-tight sm:flex">
          <span className="w-full truncate text-xs font-medium" title={userEmail ?? undefined}>
            {userEmail ?? '未登录'}
          </span>
          <span className="text-[10px] text-muted-foreground">{roleLabel}</span>
        </span>
      </DropdownMenuTrigger>

      <DropdownMenuContent side="bottom" align="end" sideOffset={6} className="w-56">
        <DropdownMenuLabel className="flex flex-col gap-0.5 py-2">
          <span className="truncate text-xs font-medium text-foreground">{userEmail ?? '未登录'}</span>
          <span className="text-[11px] font-normal text-muted-foreground">{roleLabel}</span>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />

        <DropdownMenuLabel>外观</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={theme} onValueChange={(value) => setTheme(value as ThemePreference)}>
          {THEME_OPTIONS.map(({ value, label, Icon }) => (
            <DropdownMenuRadioItem key={value} value={value}>
              <Icon aria-hidden="true" />
              {label}
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>

        <DropdownMenuSeparator />
        <DropdownMenuItem
          onClick={() => void onLogout()}
          disabled={loggingOut}
          className="text-destructive data-highlighted:bg-destructive/10 data-highlighted:text-destructive"
        >
          {loggingOut ? (
            <Loader2 className="animate-spin" aria-hidden="true" />
          ) : (
            <LogOut aria-hidden="true" />
          )}
          登出
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
