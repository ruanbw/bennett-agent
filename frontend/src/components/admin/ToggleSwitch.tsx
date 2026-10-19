import { cn } from '@/lib/utils';

export interface ToggleSwitchProps {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  /**
   * 读屏文案。开关本身没有可见文字，必须由调用方说明「切换的是什么」，
   * 否则读屏只会念「开关，已选中」，用户不知道作用于哪一行。
   */
  label: string;
  disabled?: boolean;
}

/** 管理后台内复用的开关：提供商启用状态、模型启用状态。 */
export function ToggleSwitch({ checked, onCheckedChange, label, disabled = false }: ToggleSwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onCheckedChange(!checked)}
      className={cn(
        'relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full transition-colors outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50',
        checked ? 'bg-primary' : 'bg-muted-foreground/30',
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          'inline-block size-4 rounded-full bg-background shadow-sm transition-transform',
          checked ? 'translate-x-4' : 'translate-x-0.5',
        )}
      />
    </button>
  );
}
