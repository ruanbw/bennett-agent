/**
 * 「性能与用量」偏好：输入框下方统计信息的详细程度。
 *
 * 两档：
 *   - `detailed`（详细）：轮次数/步数/输出速度、token 总量/缓存命中都可点开看明细；
 *   - `compact`（简洁）：只留输出速度与缓存命中率，不弹面板、不显示上下文明细。
 *
 * 默认 `detailed`：这些数字是「这一轮花了多少」的唯一证据，先给全再让人收窄。
 *
 * 与 `detail-level.ts` 是**两件事**，不要合并：
 *   - `detail-level` 决定「过程」（思考、工具调用）展开还是收起；
 *   - `performance-usage` 决定「统计读数」显示到什么程度。
 * 想只看答案的人会同时选「简洁/简洁」，但这是两个独立意图，捆在一起会让
 * 「我要看过程细节，但不想看 token 账本」这种组合无法表达。
 */

export type PerformanceUsage = 'detailed' | 'compact';

/** 默认档位。 */
export const DEFAULT_PERFORMANCE_USAGE: PerformanceUsage = 'detailed';

/** 存储键。 */
export const PERFORMANCE_USAGE_STORAGE_KEY = 'conversation_performance_usage';

/** 白名单：未知值一律回落默认，避免历史脏值让界面停在两种模式之外。 */
export function normalizePerformanceUsage(value: unknown): PerformanceUsage {
  return value === 'compact' || value === 'detailed' ? value : DEFAULT_PERFORMANCE_USAGE;
}

/** localStorage 的最小接口：只用到 getItem/setItem，方便单测注入假实现。 */
export interface PerformanceUsageStorage {
  getItem: (key: string) => string | null;
  setItem: (key: string, value: string) => void;
}

/** 取 localStorage；访问本身抛异常时返回 null（与 `detail-level.ts` 同口径）。 */
export function getPerformanceUsageStorage(): PerformanceUsageStorage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage;
  } catch (err) {
    console.warn('localStorage 不可用，性能与用量偏好不会被持久化:', err);
    return null;
  }
}

/** 读偏好：读不到、读到脏值、存储抛异常都回落默认。 */
export function readPerformanceUsage(
  storage: PerformanceUsageStorage | null = getPerformanceUsageStorage()
): PerformanceUsage {
  if (!storage) return DEFAULT_PERFORMANCE_USAGE;
  try {
    return normalizePerformanceUsage(storage.getItem(PERFORMANCE_USAGE_STORAGE_KEY));
  } catch (err) {
    console.warn('读取性能与用量偏好失败，回落默认:', err);
    return DEFAULT_PERFORMANCE_USAGE;
  }
}

/** 写偏好：写失败只记日志——模式已经切换生效，不该把存储异常抛给调用方。 */
export function writePerformanceUsage(
  mode: PerformanceUsage,
  storage: PerformanceUsageStorage | null = getPerformanceUsageStorage()
): void {
  if (!storage) return;
  try {
    storage.setItem(PERFORMANCE_USAGE_STORAGE_KEY, mode);
  } catch (err) {
    console.warn('保存性能与用量偏好失败:', err);
  }
}

/**
 * `storage` 事件 → 需要的档位；与本键无关的事件返回 null（调用方据此忽略）。
 *
 * `event.key === null` 表示 `storage.clear()`：偏好被清掉，同样回落默认。
 */
export function performanceUsageFromStorageEvent(
  event: Pick<StorageEvent, 'key' | 'newValue'>
): PerformanceUsage | null {
  if (event.key !== null && event.key !== PERFORMANCE_USAGE_STORAGE_KEY) return null;
  return normalizePerformanceUsage(event.newValue);
}

/** 档位元数据（文案与顺序）。图标映射留在组件层——`lib` 是纯 TS，不引 React 与图标库。 */
export const PERFORMANCE_USAGE_OPTIONS = [
  { value: 'compact' as const, label: '简洁', description: '只显示输出速度与缓存命中率' },
  { value: 'detailed' as const, label: '详细', description: '显示轮次、步数、token 用量，可点开看明细' },
] as const;
