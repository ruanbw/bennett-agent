"use client";

import { Select as SelectPrimitive } from "@base-ui/react/select";
import { Check, ChevronDown } from "lucide-react";

import { cn } from "@/lib/utils";

/**
 * 下拉选择（base-ui Select）。用于「每页条数」「厂商预设」这类有限选项。
 *
 * 为什么不用原生 `<select>`：原生下拉里每个 `<option>` 没法放图标、副标题，也没法
 * 分组样式化（厂商预设带分组与说明）；而且深色模式下的原生弹层由系统绘制，
 * 与整套令牌不搭。base-ui 的 Select 保留原生键盘语义（上下键、首字母、Esc）。
 */

function SelectRoot<Value, Multiple extends boolean | undefined = false>(
  props: SelectPrimitive.Root.Props<Value, Multiple>,
) {
  return <SelectPrimitive.Root data-slot="select" {...props} />;
}

const triggerClassName = cn(
  "flex h-8 w-full min-w-0 cursor-pointer items-center justify-between gap-2 rounded-lg border border-transparent",
  "bg-muted/60 px-2.5 py-1 text-xs text-foreground transition-colors outline-none",
  "hover:bg-muted focus-visible:border-ring focus-visible:bg-background focus-visible:ring-1 focus-visible:ring-ring/50",
  "data-disabled:pointer-events-none data-disabled:opacity-50",
);

function SelectTrigger({ className, children, ...props }: SelectPrimitive.Trigger.Props) {
  return (
    <SelectPrimitive.Trigger className={cn(triggerClassName, className)} {...props}>
      {children}
      <SelectPrimitive.Icon className="shrink-0 text-muted-foreground">
        <ChevronDown className="size-3.5" aria-hidden="true" />
      </SelectPrimitive.Icon>
    </SelectPrimitive.Trigger>
  );
}

function SelectValue({ className, ...props }: SelectPrimitive.Value.Props) {
  return (
    <SelectPrimitive.Value
      className={cn("min-w-0 truncate text-left data-placeholder:text-muted-foreground", className)}
      {...props}
    />
  );
}

function SelectContent({
  className,
  children,
  sideOffset = 4,
  ...props
}: SelectPrimitive.Popup.Props & Pick<SelectPrimitive.Positioner.Props, "sideOffset">) {
  return (
    <SelectPrimitive.Portal>
      <SelectPrimitive.Positioner sideOffset={sideOffset} alignItemWithTrigger={false} className="z-50 isolate">
        <SelectPrimitive.Popup
          className={cn(
            "min-w-[var(--anchor-width)] origin-[var(--transform-origin)] rounded-lg border border-border bg-popover p-1 text-popover-foreground shadow-lg ring-1 ring-foreground/5",
            "transition-[scale,opacity] duration-100 ease-out data-ending-style:scale-[0.98] data-ending-style:opacity-0 data-starting-style:scale-[0.98] data-starting-style:opacity-0",
            className,
          )}
          {...props}
        >
          <SelectPrimitive.List className="max-h-[var(--available-height)] scroll-py-1 overflow-y-auto">
            {children}
          </SelectPrimitive.List>
        </SelectPrimitive.Popup>
      </SelectPrimitive.Positioner>
    </SelectPrimitive.Portal>
  );
}

function SelectItem({ className, children, ...props }: SelectPrimitive.Item.Props) {
  return (
    <SelectPrimitive.Item
      className={cn(
        "grid cursor-pointer grid-cols-[0.875rem_1fr] items-center gap-1.5 rounded-md py-1.5 pr-2 pl-2 text-xs outline-none select-none",
        "data-highlighted:bg-accent data-highlighted:text-accent-foreground data-disabled:pointer-events-none data-disabled:opacity-50",
        className,
      )}
      {...props}
    >
      <SelectPrimitive.ItemIndicator className="col-start-1 flex">
        <Check className="size-3" aria-hidden="true" />
      </SelectPrimitive.ItemIndicator>
      <SelectPrimitive.ItemText className="col-start-2">{children}</SelectPrimitive.ItemText>
    </SelectPrimitive.Item>
  );
}

function SelectGroup(props: SelectPrimitive.Group.Props) {
  return <SelectPrimitive.Group className="py-0.5" {...props} />;
}

function SelectGroupLabel({ className, ...props }: SelectPrimitive.GroupLabel.Props) {
  return (
    <SelectPrimitive.GroupLabel
      className={cn("px-2 py-1 text-[11px] font-medium text-muted-foreground", className)}
      {...props}
    />
  );
}

export {
  SelectRoot as Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
  SelectGroup,
  SelectGroupLabel,
};
