/**
 * 侧边栏底部账号区，参照 DeepSeek 官方 UI：与上方列表之间没有分割线，视觉上融为一体。
 *
 * 菜单内容（设置 / 主题 / 登出）在 `AccountMenu` 里与「收起侧边栏后的胶囊
 * 工具栏头像」共用——两处各写一份必然漂移，而登出入口少一处就是功能缺失。
 */

import { MoreHorizontal } from 'lucide-react';

import { Avatar, AvatarFallback } from './ui/avatar';
import { AccountMenu } from './AccountMenu';

export interface AccountBarProps {
  email: string | null;
  onLogout: () => void | Promise<unknown>;
  collapsed?: boolean;
  /**
   * 打开设置页。
   *
   * 设置页挂在 `App` 而不是这里：侧边栏收起时本组件整块不渲染，顶栏胶囊工具栏的
   * 齿轮与头像菜单都要能打开它，因此宿主必须是常驻的父层。
   */
  onOpenSettings: () => void;
}

export function AccountBar({
  email,
  onLogout,
  collapsed = false,
  onOpenSettings,
}: AccountBarProps) {
  const nickname = email ? email.split('@')[0] : '未登录';
  const initial = email ? nickname.charAt(0).toUpperCase() : '未';

  return (
    <div className={collapsed ? 'p-2 flex justify-center' : 'px-2.5 py-2'}>
      <AccountMenu
        collapsed={collapsed}
        email={email}
        onLogout={onLogout}
        onOpenSettings={onOpenSettings}
        trigger={
          <button
            type="button"
            className={
              collapsed
                ? 'size-9 flex items-center justify-center rounded-full hover:bg-accent/60 transition cursor-pointer outline-none focus-visible:ring-1 focus-visible:ring-ring'
                : 'group w-full flex items-center gap-2.5 px-2 py-1.5 rounded-xl hover:bg-accent/40 transition text-left cursor-pointer outline-none focus-visible:ring-1 focus-visible:ring-ring'
            }
            aria-label="用户账户与设置菜单"
            title={collapsed ? `${nickname} (${email ?? '未登录'})` : undefined}
          >
            <Avatar size="sm" className="border-0 shrink-0">
              <AvatarFallback className="bg-foreground/5 text-foreground/70 text-[11px] font-semibold">
                {initial}
              </AvatarFallback>
            </Avatar>

            {!collapsed && (
              <>
                <span className="min-w-0 flex-1 text-[13px] font-medium text-foreground/85 truncate select-none">
                  {nickname}
                </span>
                <MoreHorizontal
                  className="size-4 text-muted-foreground/50 shrink-0 group-hover:text-muted-foreground transition-colors"
                  aria-hidden="true"
                />
              </>
            )}
          </button>
        }
      />
    </div>
  );
}
