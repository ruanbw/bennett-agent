/**
 * 账号菜单：侧边栏底部与「收起侧边栏」胶囊工具栏共用同一个实现。
 *
 * ## 为什么要能放进胶囊工具栏
 *
 * 侧边栏收起后 `AccountBar` 整块不渲染，于是**登出、管理后台入口**就没了；设置面板
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
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from './ui/dropdown-menu';
import { useTheme } from '../context/theme-context';
import { countPending } from '../lib/sync/engine';

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
  const [unsynced, setUnsynced] = useState(0);
  const [loggingOut, setLoggingOut] = useState(false);
  const { theme, setTheme } = useTheme();

  const nickname = email ? email.split('@')[0] : '未登录';

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

          <DropdownMenuItem onClick={() => onOpenSettings()}>
            <Settings className="size-4 text-muted-foreground" />
            <span>设置</span>
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
    </>
  );
}
