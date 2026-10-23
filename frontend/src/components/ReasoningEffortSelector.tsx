import type { ReactNode } from 'react';

export interface ReasoningEffortSelectorProps {
  /** 可选等级（含 `'off'`）。保留在 props 里只为不打断既有调用点。 */
  levels?: string[];
  currentEffort?: string;
  onSelectEffort: (effort: string) => void;
  disabled?: boolean;
}

/**
 * 已合并进 `ModelSelector`：思考等级不再是独立按钮。
 *
 * DSH 的 composer 模型位是一个触发器 + 两级菜单（`模型` / `思考等级`），本项目此前是
 * 「模型」与「思考等级」两个并排按钮。两个并排控件在窄屏上把发送按钮挤到第二行，
 * 而且用户得先猜哪个按钮管什么。合并后等级是菜单里的第二行，触发器上仍能看到当前值。
 *
 * 这个文件与导出继续存在，只是为了**不打断引用**（`thread.aui.tsx` 的接线由 Lead
 * 统一改）：保留一个返回 `null` 的薄封装，任何尚未清理的调用点编译与渲染都不会炸。
 *
 * 刻意不做「降级成独立按钮」的实现：那会留下第二套等级菜单的样式与键盘逻辑，两边迟早
 * 长成不一样（一个显示「关闭」、另一个显示 `off`）。要单独用等级控件时，从
 * `@/lib/model-menu` 的 `effortLabel` / `cycleIndex` 取规则，并把新控件放进
 * `ModelSelector` 的 `pane === 'effort'` 分支里。
 */
export function ReasoningEffortSelector(_props: ReasoningEffortSelectorProps): ReactNode {
  return null;
}
