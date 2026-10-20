/**
 * 提示条渲染层：固定视口底部，不拦截点击（除了每条自己的关闭按钮）。
 *
 * 可达性：
 * - 绝不用 `role="alert"` 的容器包住所有提示——那会把每条都当紧急打断。
 *   逐条用 `role="status"`（隐式 polite）与 `role="alert"`（仅错误，assertive）。
 * - 关闭按钮有可读名字；图标全部 `aria-hidden`。
 * - 不自动聚焦：提示不应该抢走用户正在输入的焦点。
 */

import { CheckCircle2, CircleAlert, Info, X } from 'lucide-react';
import { useSyncExternalStore } from 'react';

import { cn } from '../lib/utils';
import { dismissToast, getToasts, subscribeToasts, type ToastTone } from '../lib/toast';

const TONE_STYLES: Record<ToastTone, { icon: typeof Info; className: string }> = {
  success: {
    icon: CheckCircle2,
    className: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  },
  info: {
    icon: Info,
    className: 'border-border bg-popover text-foreground',
  },
  error: {
    icon: CircleAlert,
    className: 'border-destructive/40 bg-destructive/10 text-destructive',
  },
};

export function Toaster() {
  const toasts = useSyncExternalStore(subscribeToasts, getToasts);
  if (toasts.length === 0) return null;

  return (
    <div
      // pointer-events-none：容器铺满底部但不挡操作；每条自己恢复可点
      className="pointer-events-none fixed inset-x-0 bottom-4 z-[60] flex flex-col items-center gap-2 px-4 sm:inset-x-auto sm:right-4 sm:items-end"
    >
      {toasts.map((toast) => {
        const { icon: Icon, className } = TONE_STYLES[toast.tone];
        return (
          <div
            key={toast.id}
            role={toast.tone === 'error' ? 'alert' : 'status'}
            className={cn(
              'pointer-events-auto flex w-full max-w-sm items-start gap-2 rounded-xl border px-3 py-2 text-xs shadow-lg',
              className,
            )}
          >
            <Icon className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
            <span className="min-w-0 flex-1 break-words">{toast.message}</span>
            <button
              type="button"
              onClick={() => dismissToast(toast.id)}
              className="shrink-0 cursor-pointer opacity-60 transition hover:opacity-100"
              aria-label="关闭提示"
            >
              <X className="size-3.5" aria-hidden="true" />
            </button>
          </div>
        );
      })}
    </div>
  );
}
