"use client";

import { Checkbox as CheckboxPrimitive } from "@base-ui/react/checkbox";
import { Minus } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * 勾选框（base-ui Checkbox）。表格多选（含全选半选态）与表单布尔项共用。
 *
 * 半选态：`data-indeterminate` 时画横杠。全选必须能区分「全选」「半选」「未选」三种
 * 状态，否则用户看到 3/20 的勾会以为已经全选了。半选标记挂在 root 的 `group` 上，
 * 由子元素用 `group-data-indeterminate:` 响应。
 */
function Checkbox({ className, ...props }: CheckboxPrimitive.Root.Props) {
  return (
    <CheckboxPrimitive.Root
      data-slot="checkbox"
      className={cn(
        "group inline-flex size-4 shrink-0 cursor-pointer items-center justify-center rounded-[4px] border border-input bg-background transition-colors outline-none",
        "hover:border-ring focus-visible:border-ring focus-visible:ring-1 focus-visible:ring-ring/50",
        "data-checked:border-primary data-checked:bg-primary data-checked:text-primary-foreground",
        "data-indeterminate:border-primary data-indeterminate:bg-primary data-indeterminate:text-primary-foreground",
        "data-disabled:cursor-not-allowed data-disabled:opacity-50",
        className,
      )}
      {...props}
    >
      <CheckboxPrimitive.Indicator className="flex group-data-indeterminate:hidden">
        <svg
          viewBox="0 0 16 16"
          fill="none"
          stroke="currentColor"
          strokeWidth={2.5}
          className="size-3"
          aria-hidden="true"
        >
          <path d="m3 8.5 3.5 3.5L13 4.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </CheckboxPrimitive.Indicator>
      <Minus className="hidden size-3 group-data-indeterminate:block" aria-hidden="true" />
    </CheckboxPrimitive.Root>
  );
}

export { Checkbox };
