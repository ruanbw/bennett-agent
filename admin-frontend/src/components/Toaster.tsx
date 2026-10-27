"use client";

/**
 * 提示条渲染层：右上角（顶栏下方），容器不拦截点击，每条自己可点。
 *
 * 可达性由 base-ui 保证（`role="status"` 逐条、优先级高的用 assertive）；
 * 这里只负责样式与图标。不要用一个 `role="alert"` 的容器包住全部提示——那会把
 * 每条都当紧急打断。
 */

import { Toast as ToastPrimitive } from "@base-ui/react/toast";
import { CircleAlert, CircleCheck, Info, X } from "lucide-react";

import { cn } from "@/lib/utils";
import { toastManager, type ToastTone } from "@/lib/notify";

const TONE_STYLES: Record<ToastTone, { icon: typeof Info; className: string }> = {
  success: { icon: CircleCheck, className: "text-emerald-600 dark:text-emerald-400" },
  info: { icon: Info, className: "text-muted-foreground" },
  error: { icon: CircleAlert, className: "text-destructive" },
};

export function Toaster() {
  return (
    <ToastPrimitive.Provider toastManager={toastManager} limit={3}>
      <ToastPrimitive.Portal>
        <ToastPrimitive.Viewport className="pointer-events-none fixed top-14 right-4 z-[60] flex w-[min(24rem,calc(100vw-2rem))] flex-col items-end gap-2">
          <ToastList />
        </ToastPrimitive.Viewport>
      </ToastPrimitive.Portal>
    </ToastPrimitive.Provider>
  );
}

function ToastList() {
  const { toasts } = ToastPrimitive.useToastManager();

  return toasts.map((toast) => {
    const tone = (toast.type ?? "info") as ToastTone;
    const { icon: Icon, className } = TONE_STYLES[tone] ?? TONE_STYLES.info;

    return (
      <ToastPrimitive.Root
        key={toast.id}
        toast={toast}
        swipeDirection="right"
        className={cn(
          "pointer-events-auto w-full rounded-lg border border-border bg-popover px-3 py-2 text-xs text-popover-foreground shadow-lg",
          "transition-[transform,opacity] duration-200 data-starting-style:translate-x-2 data-starting-style:opacity-0",
          "data-ending-style:translate-x-2 data-ending-style:opacity-0 data-limited:opacity-0",
        )}
      >
        <ToastPrimitive.Content className="flex items-start gap-2">
          <Icon className={cn("mt-px size-3.5 shrink-0", className)} aria-hidden="true" />
          <ToastPrimitive.Title className="min-w-0 flex-1 leading-relaxed break-words" />
          <ToastPrimitive.Close
            aria-label="关闭提示"
            className="shrink-0 cursor-pointer rounded opacity-50 transition hover:opacity-100 focus-visible:ring-1 focus-visible:ring-ring focus-visible:outline-none"
          >
            <X className="size-3.5" aria-hidden="true" />
          </ToastPrimitive.Close>
        </ToastPrimitive.Content>
      </ToastPrimitive.Root>
    );
  });
}
