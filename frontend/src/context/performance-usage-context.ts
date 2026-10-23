/**
 * 「性能与用量」偏好的上下文。
 *
 * 与 `detail-level-context.ts` 同一形态：纯逻辑（白名单、存储读写、storage 事件解析）
 * 在 `lib/performance-usage.ts`，这里只负责把值递给组件树。
 *
 * 为什么需要 provider 而不是让 `ComposerStats` 各自读 localStorage：设置页要能改它，
 * 而设置页与输入框底部统计是两个互不相邻的组件。各自读 localStorage 会得到两个
 * 互不通知的副本——改完设置要么得刷新页面，要么得等下一次 storage 事件（同标签页内
 * 根本不会触发）。一个 provider 让「改」与「看」共享同一份状态。
 *
 * 消费者：
 *   - 设置页的「性能与用量」两档（唯一写入入口）；
 *   - 输入框下方那排统计药丸（`ComposerStats`）。
 */

import { createContext, useContext, useState } from 'react';

import {
  readPerformanceUsage,
  writePerformanceUsage,
  type PerformanceUsage,
} from '../lib/performance-usage';

export interface PerformanceUsageContextValue {
  mode: PerformanceUsage;
  setMode: (mode: PerformanceUsage) => void;
}

/**
 * 默认值是 `null` 而不是「一份默认偏好的普通对象」。
 *
 * 消费方据此区分「外面确实挂了 provider」与「组件被单独渲染（测试 / 将来别处复用）」：
 * 前者以 provider 为准，后者退回自己读 localStorage。若这里给一个带默认值 mock，
 * 组件永远分不清这两种情况，独立渲染时就会无视用户已存的偏好、一律显示「详细」。
 */
export const PerformanceUsageContext = createContext<PerformanceUsageContextValue | null>(null);

/** 读偏好；没有 provider 时返回 `null`（调用方自己回落 localStorage）。 */
export function usePerformanceUsage(): PerformanceUsageContextValue | null {
  return useContext(PerformanceUsageContext);
}

/**
 * 在「有 provider」与「没有 provider」两种情况下都能安全使用的读写入口。
 *
 * 设置页与统计药丸都该用它，而不是自己处理 `null`：
 *   - 有 provider → 直接透传（改了立刻全局生效）；
 *   - 没有 provider（组件被单独渲染 / 服务端渲染测试）→ 退回 localStorage 读写。
 *     读一次即定，写只落存储。这条路径下的「不联动」是可接受的：没有 provider 就没有
 *     第二个消费者，谈不上同步。
 *
 * 返回的 `setMode` 引用稳定（provider 的 setter 是稳定的，回退分支用同一个模块级函数）。
 */
export function usePerformanceUsageState(): PerformanceUsageContextValue {
  const fromContext = useContext(PerformanceUsageContext);
  // 没有 provider 时读一次存储。这里是 `useState` 的惰性初始化，服务端渲染时
  // `readPerformanceUsage()` 走的是 `getPerformanceUsageStorage()` 的 `typeof window`
  // 检查，返回默认值，不会抛错。
  const [storedMode] = useState<PerformanceUsage>(() => readPerformanceUsage());
  return fromContext ?? { mode: storedMode, setMode: writePerformanceUsage };
}
