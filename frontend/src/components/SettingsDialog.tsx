import { Check, Laptop, Moon, Sun, User, Info, HardDrive, KeyRound } from 'lucide-react';

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from './ui/dialog';
import { Button } from './ui/button';
import { Avatar, AvatarFallback } from './ui/avatar';
import { useTheme, type ThemePreference } from '../context/theme-context';
import { useLocalDataStats } from '../hooks/use-local-stats';
import { PasswordChangeForm } from './PasswordChangeForm';
import { ROLE_LABELS } from '../lib/admin';
import { APP_VERSION } from '../lib/app-info';
import { formatStorageUsage } from '../lib/local-stats';
import { describeToolCount, describeWebSearchCapability } from '../lib/capabilities';
import type { Capabilities } from '../types';

export interface SettingsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  userEmail: string | null;
  /** 当前账号角色（`user` / `admin` / `superadmin`）。未知/未传时不显示徽标。 */
  userRole?: string | null;
  /** 密码修改成功后调用：服务端已失效所有会话，App 负责退回登录页。 */
  onPasswordChanged?: () => void;
  /** 运行时能力快照；null = 还没拿到 / 拿不到（面板据此显示「未知」）。 */
  capabilities?: Capabilities | null;
}

export interface SettingsPanelProps {
  userEmail: string | null;
  userRole?: string | null;
  onPasswordChanged?: () => void;
  /** 运行时能力快照；null = 还没拿到 / 拿不到（面板据此显示「未知」）。 */
  capabilities?: Capabilities | null;
  /**
   * 面板是否可见。默认 true（直接渲染 `SettingsPanel` 的场景）。
   * 本机数据只在打开时读一次，不是实时监控。
   */
  open?: boolean;
}

const THEME_OPTIONS: Array<{
  value: ThemePreference;
  label: string;
  description: string;
  Icon: typeof Sun;
}> = [
  {
    value: 'light',
    label: '浅色',
    description: '保持明亮清爽的界面',
    Icon: Sun,
  },
  {
    value: 'dark',
    label: '深色',
    description: '降低暗光环境下的视觉疲劳',
    Icon: Moon,
  },
  {
    value: 'system',
    label: '跟随系统',
    description: '自动同步操作系统主题',
    Icon: Laptop,
  },
];

/**
 * 设置面板内容组件：支持独立复用与服务端渲染测试。
 */
export function SettingsPanel({
  userEmail,
  userRole = null,
  open = true,
  onPasswordChanged,
  capabilities = null,
}: SettingsPanelProps) {
  const { theme, setTheme } = useTheme();
  const stats = useLocalDataStats(open);

  // 从邮箱派生昵称与头像首字母
  const nickname = userEmail ? userEmail.split('@')[0] : '未登录用户';
  const initial = userEmail ? nickname.charAt(0).toUpperCase() : '未';
  // 未知角色（后端新增了一档）不猜，宁可只显示邮箱
  const roleLabel = userRole ? (ROLE_LABELS[userRole] ?? null) : null;

  return (
    <div className="space-y-5 py-1">
      {/* 账号信息区块 */}
      <div className="space-y-2">
        <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground flex items-center gap-1.5">
          <User className="size-3.5" />
          <span>当前账号</span>
        </h4>
        <div className="flex items-center gap-3 rounded-lg border border-border/70 bg-muted/40 p-3">
          <Avatar size="default" className="border border-border/80">
            <AvatarFallback className="bg-primary/10 text-primary font-medium text-xs">
              {initial}
            </AvatarFallback>
          </Avatar>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5">
              <span className="text-xs font-medium text-foreground truncate">{nickname}</span>
              {roleLabel && (
                <span className="shrink-0 rounded-full bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary">
                  {roleLabel}
                </span>
              )}
            </div>
            <div className="text-[11px] text-muted-foreground truncate" title={userEmail ?? undefined}>
              {userEmail ?? '未登录'}
            </div>
          </div>
        </div>
      </div>

      {/* 外观设置区块 */}
      <div className="space-y-2">
        <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground flex items-center gap-1.5">
          <Sun className="size-3.5" />
          <span>外观与主题</span>
        </h4>
        <div role="group" aria-label="外观主题" className="grid grid-cols-3 gap-2">
          {THEME_OPTIONS.map(({ value, label, description, Icon }) => {
            const isActive = theme === value;
            return (
              <button
                key={value}
                type="button"
                onClick={() => setTheme(value)}
                aria-pressed={isActive}
                className={`relative flex flex-col items-center justify-center p-3 rounded-xl border text-center transition cursor-pointer select-none focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60 ${
                  isActive
                    ? 'border-primary bg-primary/5 text-foreground shadow-xs ring-1 ring-primary/40'
                    : 'border-border/70 hover:border-border hover:bg-muted/50 text-muted-foreground'
                }`}
              >
                {isActive && (
                  <span className="absolute top-2 right-2 text-primary">
                    <Check className="size-3.5" aria-hidden="true" />
                  </span>
                )}
                <Icon
                  className={`size-5 mb-1.5 ${
                    isActive
                      ? value === 'light'
                        ? 'text-amber-500'
                        : value === 'dark'
                          ? 'text-sky-400'
                          : 'text-foreground'
                      : 'text-muted-foreground'
                  }`}
                />
                <span className="text-xs font-medium text-foreground">{label}</span>
                <span className="text-[10px] text-muted-foreground mt-0.5 leading-tight line-clamp-1">
                  {description}
                </span>
              </button>
            );
          })}
        </div>
      </div>

      {/* 修改密码：成功会让所有设备下线，由 App 统一退回登录页 */}
      <div className="space-y-2">
        <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground flex items-center gap-1.5">
          <KeyRound className="size-3.5" />
          <span>修改密码</span>
        </h4>
        <PasswordChangeForm onPasswordChanged={onPasswordChanged} />
      </div>

      {/* 本机数据区块：数据默认只存浏览器，用户应该能知道自己存了多少 */}
      <div className="space-y-2">
        <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground flex items-center gap-1.5">
          <HardDrive className="size-3.5" />
          <span>本机数据</span>
        </h4>
        <div className="rounded-lg border border-border/70 bg-muted/40 p-3 space-y-1.5 text-xs">
          <div className="flex justify-between items-center">
            <span className="text-muted-foreground">会话</span>
            <span className="tabular-nums text-foreground">
              {stats ? `${stats.sessions} 个` : '—'}
            </span>
          </div>
          <div className="flex justify-between items-center">
            <span className="text-muted-foreground">消息</span>
            <span className="tabular-nums text-foreground">
              {stats ? `${stats.messages} 条` : '—'}
            </span>
          </div>
          <div className="flex justify-between items-center">
            <span className="text-muted-foreground">存储占用</span>
            <span
              className="tabular-nums text-foreground"
              title="浏览器对整个站点的估算值，包含缓存等其他数据；括号内为估算配额占比"
            >
              {stats ? formatStorageUsage(stats.usageBytes, stats.quotaBytes) : '—'}
            </span>
          </div>
        </div>
        <p className="text-[10.5px] leading-relaxed text-muted-foreground/80">
          会话默认保存在本机浏览器；登录同一账号时后台同步到服务器。存储占用是浏览器对本站点的估算值，括号内为估算配额占比。
        </p>
      </div>

      {/* 关于 Bennett Agent 区块 */}
      <div className="space-y-2">
        <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground flex items-center gap-1.5">
          <Info className="size-3.5" />
          <span>关于系统</span>
        </h4>
        <div className="rounded-lg border border-border/70 bg-muted/40 p-3 space-y-1.5 text-xs">
          <div className="flex justify-between items-center">
            <span className="text-muted-foreground">应用版本</span>
            {/* 版本来自 package.json（构建时注入），不再手写 */}
            <span className="font-mono text-[11px] text-foreground">v{APP_VERSION}</span>
          </div>
          <div className="flex justify-between items-center">
            <span className="text-muted-foreground">推理内核</span>
            <span className="text-foreground">PydanticAI + assistant-ui</span>
          </div>
          {/* 服务端能力：空对话页的示例是裁剪过的，这里说明裁剪依据，
              否则「没有搜索示例」会被当成功能缺失，而不是服务端没配 */}
          <div className="flex justify-between items-center">
            <span className="text-muted-foreground">联网搜索</span>
            <span className="text-foreground">{describeWebSearchCapability(capabilities)}</span>
          </div>
          <div className="flex justify-between items-center">
            <span className="text-muted-foreground">已装配工具</span>
            <span className="text-foreground">{describeToolCount(capabilities)}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

export function SettingsDialog({
  open,
  onOpenChange,
  userEmail,
  userRole = null,
  capabilities = null,
  onPasswordChanged,
}: SettingsDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>设置</DialogTitle>
          <DialogDescription>管理你的外观偏好与账号信息</DialogDescription>
        </DialogHeader>

        <SettingsPanel
          userEmail={userEmail}
          userRole={userRole}
          open={open}
          capabilities={capabilities}
          onPasswordChanged={onPasswordChanged}
        />

        <DialogFooter>
          <Button variant="default" size="sm" onClick={() => onOpenChange(false)}>
            完成
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
