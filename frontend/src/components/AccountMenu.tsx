/**
 * 账号菜单：侧边栏底部与「收起侧边栏」胶囊工具栏共用同一个实现。
 *
 * ## 为什么要能放进胶囊工具栏
 *
 * 侧边栏收起后 `AccountBar` 整块不渲染，于是**登出、管理后台入口**就没了；设置页
 * 已经被提到 App 层（齿轮），但账号本身的操作仍然只挂在侧边栏底部。这里把菜单内容
 * 抽出来，两处各给一个触发器：侧边栏底部是「头像 + 昵称」，胶囊工具栏是一个图标按钮。
 *
 * ## 触发器与内容分离的理由
 *
 * 菜单内容（账号信息 / 设置 / 管理后台 / 主题 / 登出 / 登出确认框）是一整套状态与
 * 交互，复制两份必然漂移（此前「设置」就在两处各自持有面板状态）。所以这里只接受一个
 * `trigger` 节点，其余全部共享；位置差异用 `collapsed` 控制（侧边/对齐方向）。
 */

import { useCallback, useState, type ReactElement } from 'react';
import {
  Check,
  Laptop,
  Loader2,
  LogOut,
  Moon,
  Settings,
  ShieldCheck,
  Sun,
} from 'lucide-react';

import { ConfirmDialog } from './ui/confirm-dialog';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from './ui/dropdown-menu';
import { normalizeThemePreference, useTheme } from '../context/theme-context';
import { SETTINGS_SHORTCUT_KEYS, settingsShortcutLabel } from '../lib/keyboard-shortcuts';
import { logoutWarning } from '../lib/logout-warning';
import { countPending } from '../lib/sync/engine';
import { notify } from '../lib/toast';

export interface AccountMenuProps {
  /** 触发器节点（必须是可聚焦的按钮；base-ui 的 Trigger 用 render 接管它）。 */
  trigger: ReactElement;
  email: string | null;
  onLogout: () => void | Promise<unknown>;
  onOpenSettings: () => void;
  /** 是否渲染管理后台入口：只有管理员/超管能拿到。 */
  canOpenAdmin?: boolean;
  onOpenAdmin?: () => void;
  /** 收起态：菜单向右侧弹出并对齐末端（侧边栏底部则向上弹出）。 */
  collapsed?: boolean;
}

export function AccountMenu({
  trigger,
  email,
  onLogout,
  onOpenSettings,
  canOpenAdmin = false,
  onOpenAdmin,
  collapsed = false,
}: AccountMenuProps) {
  const [confirming, setConfirming] = useState(false);
  // null = 还在数；数字 = 已数清；countFailed 时数字不可信
  const [unsynced, setUnsynced] = useState<number | null>(null);
  const [countFailed, setCountFailed] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const { theme, setTheme } = useTheme();

  const nickname = email ? email.split('@')[0] : '未登录';

  const openLogoutConfirm = useCallback(() => {
    // 先把数量清空：弹窗要显示「正在检查」并禁用确认，不能在数量回来前给出
    // 「没有未同步内容」的错误结论——那是数据丢失前最后一句提示。
    setUnsynced(null);
    setCountFailed(false);
    setConfirming(true);
    void countPending()
      .then((count) => setUnsynced(count))
      .catch((err) => {
        console.warn('读取待同步数量失败:', err);
        setCountFailed(true);
      });
  }, []);

  const handleLogout = useCallback(async () => {
    setLoggingOut(true);
    try {
      await onLogout();
    } catch (err) {
      // useAuth.logout 自身不 reject（服务端未确认会作为 error 带回登录页）；
      // 这里兜住外部注入实现抛错的情况，不能让菜单关掉后界面零反馈。
      console.error('登出失败:', err);
      notify('error', '登出失败，请重试');
    } finally {
      setLoggingOut(false);
    }
  }, [onLogout]);

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger render={trigger} />
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

          <DropdownMenuItem
            onClick={() => onOpenSettings()}
            aria-keyshortcuts={SETTINGS_SHORTCUT_KEYS}
            className="justify-between"
          >
            <span className="flex items-center gap-2">
              <Settings className="size-4 text-muted-foreground" />
              <span>设置</span>
            </span>
            <span className="font-mono text-[10px] text-muted-foreground/60">
              {settingsShortcutLabel()}
            </span>
          </DropdownMenuItem>

          {/* 入口只对管理员可见；权限本身仍由后端裁决 */}
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
            <DropdownMenuRadioGroup
              value={theme}
              onValueChange={(value) => setTheme(normalizeThemePreference(value))}
            >
              <DropdownMenuRadioItem
                value="light"
                closeOnClick
                className="justify-between"
              >
                <div className="flex items-center gap-2.5">
                  <Sun className="size-4 text-amber-500" />
                  <span>浅色</span>
                </div>
                {theme === 'light' && <Check className="size-3.5 text-primary" />}
              </DropdownMenuRadioItem>
              <DropdownMenuRadioItem
                value="dark"
                closeOnClick
                className="justify-between"
              >
                <div className="flex items-center gap-2.5">
                  <Moon className="size-4 text-sky-400" />
                  <span>深色</span>
                </div>
                {theme === 'dark' && <Check className="size-3.5 text-primary" />}
              </DropdownMenuRadioItem>
              <DropdownMenuRadioItem
                value="system"
                closeOnClick
                className="justify-between"
              >
                <div className="flex items-center gap-2.5">
                  <Laptop className="size-4 text-muted-foreground" />
                  <span>跟随系统</span>
                </div>
                {theme === 'system' && <Check className="size-3.5 text-primary" />}
              </DropdownMenuRadioItem>
            </DropdownMenuRadioGroup>
          </DropdownMenuGroup>

          <DropdownMenuSeparator />

          <DropdownMenuItem
            onClick={openLogoutConfirm}
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
        description={logoutWarning(unsynced, countFailed)}
        confirmLabel="登出"
        destructive
        // 数量还没数清就不许确认：默认文案不能替用户宣布「没有未同步内容」
        confirmDisabled={unsynced === null && !countFailed}
        onConfirm={() => {
          setConfirming(false);
          void handleLogout();
        }}
      />
    </>
  );
}
