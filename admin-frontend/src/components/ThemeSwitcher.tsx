import { Laptop, Moon, Sun, type LucideIcon } from 'lucide-react';

import { useTheme, type ThemePreference } from '../context/theme-context';

interface ThemeOption {
  value: ThemePreference;
  label: string;
  Icon: LucideIcon;
  activeClass: string;
}

const OPTIONS: ThemeOption[] = [
  { value: 'light', label: '浅色', Icon: Sun, activeClass: 'text-amber-500' },
  { value: 'system', label: '系统', Icon: Laptop, activeClass: 'text-foreground' },
  { value: 'dark', label: '深色', Icon: Moon, activeClass: 'text-sky-400' },
];

/**
 * 三态主题切换。
 *
 * 从 Sidebar 里拆出（原文件同时承担会话 CRUD、搜索、多选、批量操作与主题切换，
 * 有五个变更理由）。作为独立组件它也更容易被复用与测试。
 */
export function ThemeSwitcher() {
  const { theme, setTheme } = useTheme();

  return (
    <div
      role="group"
      aria-label="外观主题"
      className="flex items-center justify-between bg-muted/60 p-1 rounded-lg border border-border/40"
    >
      {OPTIONS.map(({ value, label, Icon, activeClass }) => {
        const isActive = theme === value;
        return (
          <button
            key={value}
            type="button"
            onClick={() => setTheme(value)}
            title={`${label}模式`}
            aria-label={`${label}模式`}
            aria-pressed={isActive}
            className={`flex-1 flex items-center justify-center gap-1.5 py-1.5 px-2 rounded-md text-xs font-medium transition cursor-pointer focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60 ${
              isActive ? 'bg-background text-foreground shadow-xs' : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            <Icon size={14} className={isActive ? activeClass : ''} aria-hidden="true" />
            <span>{label}</span>
          </button>
        );
      })}
    </div>
  );
}
