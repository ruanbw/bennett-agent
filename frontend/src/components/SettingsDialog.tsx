import { Check, Laptop, Moon, Sun, User, Info } from 'lucide-react';

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

export interface SettingsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  userEmail: string | null;
}

export interface SettingsPanelProps {
  userEmail: string | null;
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
export function SettingsPanel({ userEmail }: SettingsPanelProps) {
  const { theme, setTheme } = useTheme();

  // 从邮箱派生昵称与头像首字母
  const nickname = userEmail ? userEmail.split('@')[0] : '未登录用户';
  const initial = userEmail ? nickname.charAt(0).toUpperCase() : '未';

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
            <div className="text-xs font-medium text-foreground truncate">{nickname}</div>
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
        <div className="grid grid-cols-3 gap-2">
          {THEME_OPTIONS.map(({ value, label, description, Icon }) => {
            const isActive = theme === value;
            return (
              <button
                key={value}
                type="button"
                onClick={() => setTheme(value)}
                className={`relative flex flex-col items-center justify-center p-3 rounded-xl border text-center transition cursor-pointer select-none ${
                  isActive
                    ? 'border-primary bg-primary/5 text-foreground shadow-xs ring-1 ring-primary/40'
                    : 'border-border/70 hover:border-border hover:bg-muted/50 text-muted-foreground'
                }`}
              >
                {isActive && (
                  <span className="absolute top-2 right-2 text-primary">
                    <Check className="size-3.5" />
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

      {/* 关于 Bennett Agent 区块 */}
      <div className="space-y-2">
        <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground flex items-center gap-1.5">
          <Info className="size-3.5" />
          <span>关于系统</span>
        </h4>
        <div className="rounded-lg border border-border/70 bg-muted/40 p-3 space-y-1.5 text-xs">
          <div className="flex justify-between items-center">
            <span className="text-muted-foreground">应用版本</span>
            <span className="font-mono text-[11px] text-foreground">v0.1.0</span>
          </div>
          <div className="flex justify-between items-center">
            <span className="text-muted-foreground">推理内核</span>
            <span className="text-foreground">PydanticAI + assistant-ui</span>
          </div>
        </div>
      </div>
    </div>
  );
}

export function SettingsDialog({ open, onOpenChange, userEmail }: SettingsDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>设置</DialogTitle>
          <DialogDescription>管理你的外观偏好与账号信息</DialogDescription>
        </DialogHeader>

        <SettingsPanel userEmail={userEmail} />

        <DialogFooter>
          <Button variant="default" size="sm" onClick={() => onOpenChange(false)}>
            完成
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
