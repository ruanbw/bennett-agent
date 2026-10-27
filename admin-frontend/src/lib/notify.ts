"use client";

/**
 * 提示（toast）：一次动作的反馈，不承载持续状态。
 *
 * 分工：列表加载失败这类「刷新后仍然成立」的状态留在页面里的内联条上；只有
 * 「刚刚这一下做成了 / 没做成」（同步模型、封禁用户、删除）走 toast。
 *
 * manager 建在模块作用域：请求封装、事件处理函数外层等非 React 代码也能直接上报，
 * `<Toaster />` 只负责订阅渲染。
 */

import { Toast as ToastPrimitive } from "@base-ui/react/toast";

export type ToastTone = "success" | "info" | "error";

/** 错误停久一点：一句话读不完就消失等于没提示。 */
const TIMEOUT_MS: Record<ToastTone, number> = { success: 3_500, info: 4_500, error: 7_000 };

export const toastManager = ToastPrimitive.createToastManager();

export function notify(tone: ToastTone, message: string): void {
  toastManager.add({
    title: message,
    type: tone,
    // `id` 固定 = 同文案再次触发时原地更新并重置计时，不叠出两条一样的提示
    id: `${tone}:${message}`,
    timeout: TIMEOUT_MS[tone],
    priority: tone === "error" ? "high" : "low",
  });
}
