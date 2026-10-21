/**
 * 轻量提示（toast）：**动作反馈**，不是状态展示。
 *
 * 分工很重要：
 * - 持续状态（同步失败、落库失败、上下文已满）留在页面里的内联条上——它们刷新后仍然
 *   成立，用会自动消失的 toast 表达会让人错过。
 * - 一次动作的结果（新建会话失败、导出完成）没有「持久位置」可放，才走这里。
 *
 * 设计取舍：
 * - 状态在模块里（与 chat-store 一致），不依赖 React：任何纯模块都能报告失败，UI 只订阅。
 * - 同一条消息去重并重置计时：连点两次「新建会话失败」不该叠出两条一模一样的提示。
 * - 最多 3 条：更早的被挤掉（用户已经在看最新的一条）。
 * - 错误提示停留更久（8s），成功 4s、信息 5s——留够读完一句话的时间。
 */

export type ToastTone = 'success' | 'info' | 'error';

export interface ToastItem {
  id: string;
  tone: ToastTone;
  message: string;
}

/** 同时可见的条数上限。 */
export const MAX_TOASTS = 3;

/** 各语气的自动消失时间（毫秒）。错误必须比成功停得更久。 */
export const TOAST_DURATION_MS: Record<ToastTone, number> = {
  success: 4_000,
  info: 5_000,
  error: 8_000,
};

let sequence = 0;
let toasts: ToastItem[] = [];
const listeners = new Set<() => void>();

interface ToastTimer {
  tone: ToastTone;
  /** 暂停时为 null，避免误 clear 一个已经结束的句柄。 */
  handle: ReturnType<typeof setTimeout> | null;
  deadline: number;
  remaining: number;
}

const timers = new Map<string, ToastTimer>();

function emit(): void {
  for (const listener of [...listeners]) listener();
}

function clearTimer(id: string): void {
  const entry = timers.get(id);
  if (!entry) return;
  if (entry.handle !== null) clearTimeout(entry.handle);
  timers.delete(id);
}

function scheduleDismiss(id: string, tone: ToastTone, duration = TOAST_DURATION_MS[tone]): void {
  clearTimer(id);
  const wait = Math.max(0, duration);
  const entry: ToastTimer = {
    tone,
    deadline: Date.now() + wait,
    remaining: wait,
    handle: setTimeout(() => dismissToast(id), wait),
  };
  timers.set(id, entry);
}

function setToasts(next: ToastItem[]): void {
  toasts = next;
  emit();
}

/** 订阅变化（`useSyncExternalStore` 用）。 */
export function subscribeToasts(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** 当前提示（引用只在变化时更新，可直接作为快照）。 */
export function getToasts(): ToastItem[] {
  return toasts;
}

/** 暂停一条提示的倒计时（鼠标悬停或键盘焦点进入时调用）。 */
export function pauseToast(id: string): void {
  const entry = timers.get(id);
  if (!entry || entry.handle === null) return;
  clearTimeout(entry.handle);
  entry.handle = null;
  entry.remaining = Math.max(0, entry.deadline - Date.now());
}

/** 恢复倒计时：从暂停时剩下的时间继续，不重新给满时长。 */
export function resumeToast(id: string): void {
  const entry = timers.get(id);
  if (!entry || entry.handle !== null) return;
  scheduleDismiss(id, entry.tone, entry.remaining);
}

/**
 * 发一条提示，返回 id。
 *
 * 同语气 + 同文案视为同一条：不重复插入，只重置它的倒计时（用户刚刚又触发了一次，
 * 说明这条信息此刻仍然相关）。
 */
export function notify(tone: ToastTone, message: string): string {
  const existing = toasts.find((toast) => toast.tone === tone && toast.message === message);
  if (existing) {
    scheduleDismiss(existing.id, tone);
    return existing.id;
  }

  sequence += 1;
  const item: ToastItem = { id: `toast-${sequence}`, tone, message };
  let next = [...toasts, item];

  if (next.length > MAX_TOASTS) {
    for (const dropped of next.slice(0, next.length - MAX_TOASTS)) clearTimer(dropped.id);
    next = next.slice(-MAX_TOASTS);
  }

  scheduleDismiss(item.id, tone);
  setToasts(next);
  return item.id;
}

/** 手动关闭一条。 */
export function dismissToast(id: string): void {
  if (!toasts.some((toast) => toast.id === id)) return;
  clearTimer(id);
  setToasts(toasts.filter((toast) => toast.id !== id));
}

/** 清空（登出、测试重置）。 */
export function dismissAllToasts(): void {
  for (const id of [...timers.keys()]) clearTimer(id);
  if (toasts.length > 0) setToasts([]);
}
