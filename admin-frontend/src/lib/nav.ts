/**
 * 导航表：侧边栏菜单与面包屑的**唯一**来源。
 *
 * 之前菜单项写死在 `AdminLayout` 里，面包屑如果也自己列一份，加一页就要改两处、
 * 而且很容易只改一处（表现为“侧边栏能点进去，面包屑却显示旧标题”）。这里把
 * `path → 标题` 收敛成一张表：菜单按 `children` 展开，面包屑按路径查表。
 *
 * 不做权限过滤：能进来就是 admin 以上，每个页面内部各自按角色收窄能力（例如只有
 * superadmin 能改角色），菜单不随角色变化——后台导航跳来跳去才更让人困惑。
 */

import { LayoutDashboard, Server, Users, type LucideIcon } from 'lucide-react';

export interface NavItem {
  /** 绝对路径，必须与 `App.tsx` 的路由一致。 */
  to: string;
  label: string;
  Icon: LucideIcon;
  /** 侧边栏分组标题；同一组内相邻项归在一起。 */
  group: string;
}

export const NAV_ITEMS: NavItem[] = [
  { to: '/overview', label: '概览', Icon: LayoutDashboard, group: '总览' },
  { to: '/providers', label: '模型提供商', Icon: Server, group: '资源管理' },
  { to: '/users', label: '用户管理', Icon: Users, group: '资源管理' },
];

/** 登录后与菜单默认落点。 */
export const HOME_PATH = '/overview';

/** 按路径找菜单项；用于面包屑与页面标题，找不到返回 null。 */
export function findNavItem(pathname: string): NavItem | null {
  return NAV_ITEMS.find((item) => item.to === pathname) ?? null;
}

/**
 * 路径 → 面包屑层级。
 *
 * 只支持一层菜单（本项目没有多级目录）。仍写成数组而不是直接返回菜单项，
 * 是因为面包屑要的语义是「层级」——将来真出现二级页（`/providers/xxx`），
 * 只需在这里补一段匹配，`AdminLayout` 不用改。
 */
export interface Crumb {
  label: string;
  to?: string;
}

export function breadcrumbsFor(pathname: string): Crumb[] {
  const item = findNavItem(pathname);
  if (!item) return [{ label: '页面不存在' }];
  return [{ label: '首页', to: HOME_PATH }, { label: item.label }];
}
