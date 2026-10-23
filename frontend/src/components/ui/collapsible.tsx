"use client";

import { Collapsible as CollapsiblePrimitive } from "@base-ui/react/collapsible";

import { cn } from "@/lib/utils";

function Collapsible({ ...props }: CollapsiblePrimitive.Root.Props) {
  return <CollapsiblePrimitive.Root data-slot="collapsible" {...props} />;
}

function CollapsibleTrigger({ ...props }: CollapsiblePrimitive.Trigger.Props) {
  return (
    <CollapsiblePrimitive.Trigger data-slot="collapsible-trigger" {...props} />
  );
}

/**
 * 折叠面板。
 *
 * 高度过渡挂在 `aui-collapse-panel` 上（样式与「为什么不是 animate-collapsible-*」
 * 的说明都在 `src/index.css`）：三处面板（过程组 / 思考正文 / 工具详情）共用同一条
 * 动画，调用方不需要再各自拼 `transition-all` 与动画类——那些写法里只要漏一个变量，
 * 动画就会静默退化成瞬跳。
 */
function CollapsibleContent({
  className,
  ...props
}: CollapsiblePrimitive.Panel.Props) {
  return (
    <CollapsiblePrimitive.Panel
      data-slot="collapsible-content"
      className={cn("aui-collapse-panel", className)}
      {...props}
    />
  );
}

export { Collapsible, CollapsibleTrigger, CollapsibleContent };
