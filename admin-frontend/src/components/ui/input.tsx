import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * 单行输入框基元。
 *
 * 样式沿用同一套令牌与 focus/aria-invalid 处理，
 * 这样表单里两种控件不会出现视觉差异。
 */
function Input({ className, type, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        "placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-ring/50 disabled:bg-input/50 aria-invalid:border-destructive aria-invalid:ring-destructive/20 dark:disabled:bg-input/80 dark:aria-invalid:border-destructive/50 dark:aria-invalid:ring-destructive/40 bg-muted/60 focus-visible:bg-background h-9 w-full min-w-0 rounded-lg border border-transparent px-3 py-1 text-base transition-colors outline-none focus-visible:ring-1 disabled:pointer-events-none disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:ring-1 md:text-sm",
        className,
      )}
      {...props}
    />
  );
}

export { Input };
