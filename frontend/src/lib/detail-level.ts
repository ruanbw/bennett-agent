/**
 * 「对话显示」偏好：思考与工具调用默认展开还是默认收起。
 *
 * 两个档位：
 *   - `detailed`（详细）：思考过程与每次工具调用的参数/结果默认展开；
 *   - `concise`（简洁）：整个「过程」块收成一行摘要，只把最终回答留在外面，
 *     点一下仍可展开看细节。
 *
 * 默认 `concise`：多数时候用户只关心答案，过程是一层可选的解释。
 *
 * 与主题偏好同样的边界：存储在 localStorage，读不到/读到脏值/存储抛异常都回落默认，
 * 不让偏好影响对话本身。本文件不依赖 React，可以在 node 环境的 vitest 里直接测。
 */

export type DetailLevel = 'detailed' | 'concise';

/** 默认档位。 */
export const DEFAULT_DETAIL_LEVEL: DetailLevel = 'concise';

/** 存储键。 */
export const DETAIL_LEVEL_STORAGE_KEY = 'conversation_detail_level';

/** 白名单：未知值一律回落默认，避免历史脏值让界面停在两种模式之外。 */
export function normalizeDetailLevel(value: unknown): DetailLevel {
  return value === 'detailed' || value === 'concise' ? value : DEFAULT_DETAIL_LEVEL;
}

/** localStorage 的最小接口：只用到 getItem/setItem，方便单测注入假实现。 */
export interface DetailLevelStorage {
  getItem: (key: string) => string | null;
  setItem: (key: string, value: string) => void;
}

/** 取 localStorage；访问本身抛异常时返回 null（与 `theme-context.getThemeStorage` 同口径）。 */
export function getDetailLevelStorage(): DetailLevelStorage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage;
  } catch (err) {
    console.warn('localStorage 不可用，对话显示偏好不会被持久化:', err);
    return null;
  }
}

/** 读偏好：读不到、读到脏值、存储抛异常都回落默认。 */
export function readDetailLevel(
  storage: DetailLevelStorage | null = getDetailLevelStorage()
): DetailLevel {
  if (!storage) return DEFAULT_DETAIL_LEVEL;
  try {
    return normalizeDetailLevel(storage.getItem(DETAIL_LEVEL_STORAGE_KEY));
  } catch (err) {
    console.warn('读取对话显示偏好失败，回落默认:', err);
    return DEFAULT_DETAIL_LEVEL;
  }
}

/** 写偏好：写失败只记日志——模式已经切换生效，不该把存储异常抛给调用方。 */
export function writeDetailLevel(
  level: DetailLevel,
  storage: DetailLevelStorage | null = getDetailLevelStorage()
): void {
  if (!storage) return;
  try {
    storage.setItem(DETAIL_LEVEL_STORAGE_KEY, level);
  } catch (err) {
    console.warn('保存对话显示偏好失败:', err);
  }
}

/**
 * `storage` 事件 → 需要的档位；与本键无关的事件返回 null（调用方据此忽略）。
 *
 * `event.key === null` 表示 `storage.clear()`：偏好被清掉，同样回落默认。
 */
export function detailLevelFromStorageEvent(
  event: Pick<StorageEvent, 'key' | 'newValue'>
): DetailLevel | null {
  if (event.key !== null && event.key !== DETAIL_LEVEL_STORAGE_KEY) return null;
  return normalizeDetailLevel(event.newValue);
}

/**
 * 档位元数据（文案与顺序）。图标映射留在组件层——`lib` 是纯 TS，不引 React 与图标库。
 */
export const DETAIL_LEVEL_OPTIONS = [
  { value: 'concise' as const, label: '简洁', description: '思考与工具默认收起，只露最终回答' },
  { value: 'detailed' as const, label: '详细', description: '思考与工具调用默认展开' },
] as const;
