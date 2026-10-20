/**
 * 侧边栏底部账号区，参照 DeepSeek 官方 UI：与上方列表之间没有分割线，视觉上融为一体。
 */

import { useCallback, useState } from 'react';
import { Check, Laptop, Loader2, LogOut, Moon, MoreHorizontal, Settings, ShieldCheck, Sun } from 'lucide-react';

import { Avatar, AvatarFallback } from './ui/avatar';
import { ConfirmDialog } from './ui/confirm-dialog';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from './ui/dropdown-menu';
import { useTheme } from '../context/theme-context';
import { countPending } from '../lib/sync/engine';

export interface AccountBarProps {
  email: string | null;
  onLogout: () => void | Promise<unknown>;
  collapsed?: boolean;
  /** 是否渲染管理后台入口：只有超级管理员能拿到。 */
  canOpenAdmin?: boolean;
  onOpenAdmin?: () => void;
  /**
   * 打开设置面板。
   *
   * 设置面板挂在 `App` 而不是这里：侧边栏收起时 AccountBar 整块不渲染，面板就跟着
   * 消失。顶栏胶囊工具栏的齿轮要能打开它，因此宿主必须是常驻的父层。
   */
  onOpenSettings: () => void;
}

export function AccountBar({
  email,
  onLogout,
  collapsed = false,
  canOpenAdmin = false,
  onOpenAdmin,
  onOpenSettings,
}: AccountBarProps) {
  const [confirming, setConfirming] = useState(false);
  const [unsynced, setUnsynced] = useState(0);
  const [loggingOut, setLoggingOut] = useState(false);

  const { theme, setTheme } = useTheme();

  const handleLogout = useCallback(async () => {
    setLoggingOut(true);
    try {
      await onLogout();
    } catch (err) {
      console.error('登出失败:', err);
    } finally {
      setLoggingOut(false);
    }
  }, [onLogout]);

  const nickname = email ? email.split('@')[0] : '未登录';
  const initial = email ? nickname.charAt(0).toUpperCase() : '未';

  return (
    <div className={collapsed ? 'p-2 flex justify-center' : 'px-2.5 py-2'}>
      <DropdownMenu>
        <DropdownMenuTrigger
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
        </DropdownMenuTrigger>

        <DropdownMenuContent
          side={collapsed ? 'right' : 'top'}
          align={collapsed ? 'end' : 'start'}
          sideOffset={8}
          className="w-60 p-1.5"
        >
          <div className="px-2.5 py-2">
            <div className="text-xs font-medium text-foreground truncate">{nickname}</div>
            <div className="text-[11px] text-muted-foreground truncate mt-0.5" title={email ?? undefined}>
              {email ?? '未登录'}
            </div>
          </div>

          <DropdownMenuSeparator />

          <DropdownMenuItem onClick={() => onOpenSettings()}>
            <Settings className="size-4 text-muted-foreground" />
            <span>设置</span>
          </DropdownMenuItem>

          {/* 入口只对超级管理员可见；权限本身仍由后端裁决 */}
          {canOpenAdmin && onOpenAdmin && (
            <DropdownMenuItem onClick={() => onOpenAdmin()}>
              <ShieldCheck className="size-4 text-muted-foreground" />
              <span>管理后台</span>
            </DropdownMenuItem>
          )}

          <DropdownMenuSeparator />

          {/* Menulabel 用的是 base-ui 的 GroupLabel，必须在 Group 内——
              否则直接抛「MenuGroupContext is missing」把整棵树打挂 */}
          <DropdownMenuGroup>
            <DropdownMenuLabel>外观主题</DropdownMenuLabel>
            <DropdownMenuItem onClick={() => setTheme('light')} className="justify-between">
              <div className="flex items-center gap-2.5">
                <Sun className="size-4 text-amber-500" />
                <span>浅色</span>
              </div>
              {theme === 'light' && <Check className="size-3.5 text-primary" />}
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => setTheme('dark')} className="justify-between">
              <div className="flex items-center gap-2.5">
                <Moon className="size-4 text-sky-400" />
                <span>深色</span>
              </div>
              {theme === 'dark' && <Check className="size-3.5 text-primary" />}
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => setTheme('system')} className="justify-between">
              <div className="flex items-center gap-2.5">
                <Laptop className="size-4 text-muted-foreground" />
                <span>跟随系统</span>
              </div>
              {theme === 'system' && <Check className="size-3.5 text-primary" />}
            </DropdownMenuItem>
          </DropdownMenuGroup>

          <DropdownMenuSeparator />

          <DropdownMenuItem
            onClick={() => {
              // 打开确认框时查一次待同步行数：登出会清空本机数据，未同步的内容会永久丢失，
              // 这件事必须在用户按下「登出」之前说清楚。
              void countPending()
                .then(setUnsynced)
                .catch(() => setUnsynced(0));
              setConfirming(true);
            }}
            disabled={loggingOut}
            className="text-destructive hover:bg-destructive/10 data-[highlighted]:bg-destructive/10 data-[highlighted]:text-destructive"
          >
            {loggingOut ? (
              <Loader2 className="size-4 animate-spin" />
            ) : (
              <LogOut className="size-4" />
            )}
            <span>{loggingOut ? '正在登出...' : '退出登录'}</span>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title="登出？"
        description={
          unsynced > 0
            ? `有 ${unsynced} 条内容尚未同步到服务器，登出会清空本机数据，这些内容将无法恢复。`
            : '登出会清空本机保存的会话记录，之后需要重新输入邮箱和密码才能继续对话。'
        }
        confirmLabel="登出"
        destructive
        onConfirm={() => {
          setConfirming(false);
          void handleLogout();
        }}
      />
    </div>
  );
}
