/**
 * 「生成中发送」偏好：回复生成期间输入框能不能继续发消息。
 *
 * 两档：
 *   - `insert`（插入）：回复正在生成时点发送，消息**插入排队**，按顺序在本轮结束后
 *     依次发出；队列里的消息可以改文字、删除，也可以让它跳队立即发送。
 *   - `block`（阻塞）：保持原有行为——生成期间发送按钮禁用、Enter 不发送，草稿留在
 *     输入框里等本轮结束。
 *
 * 默认 `block`：排队改变了「点发送 = 立刻有回复在写」的直觉，是否接受这个改变由
 * 用户在设置里选；不选就是旧行为。
 *
 * 与 `detail-level.ts` / `performance-usage.ts` 同一条边界：存储在 localStorage，
 * 读不到/读到脏值/存储抛异常都回落默认，不抛给调用方。本文件不依赖 React，可以在
 * node 环境的 vitest 里直接测。
 */

export type MessageQueuePreference = 'insert' | 'block';

/** 默认档位：维持「生成期间不能发送」的既有行为。 */
export const DEFAULT_MESSAGE_QUEUE_PREF: MessageQueuePreference = 'block';

/** 存储键。 */
export const MESSAGE_QUEUE_PREF_STORAGE_KEY = 'chat_message_queue_pref';

/** 白名单：未知值一律回落默认，避免历史脏值让界面停在两档之外。 */
export function normalizeMessageQueuePref(value: unknown): MessageQueuePreference {
  return value === 'insert' || value === 'block' ? value : DEFAULT_MESSAGE_QUEUE_PREF;
}

/** localStorage 的最小接口：只用到 getItem/setItem，方便单测注入假实现。 */
export interface MessageQueuePrefStorage {
  getItem: (key: string) => string | null;
  setItem: (key: string, value: string) => void;
}

/** 取 localStorage；访问本身抛异常时返回 null（与 `detail-level.ts` 同口径）。 */
export function getMessageQueuePrefStorage(): MessageQueuePrefStorage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage;
  } catch (err) {
    console.warn('localStorage 不可用，生成中发送偏好不会被持久化:', err);
    return null;
  }
}

/** 读偏好：读不到、读到脏值、存储抛异常都回落默认。 */
export function readMessageQueuePref(
  storage: MessageQueuePrefStorage | null = getMessageQueuePrefStorage()
): MessageQueuePreference {
  if (!storage) return DEFAULT_MESSAGE_QUEUE_PREF;
  try {
    return normalizeMessageQueuePref(storage.getItem(MESSAGE_QUEUE_PREF_STORAGE_KEY));
  } catch (err) {
    console.warn('读取生成中发送偏好失败，回落默认:', err);
    return DEFAULT_MESSAGE_QUEUE_PREF;
  }
}

/** 写偏好：写失败只记日志——模式已经切换生效，不该把存储异常抛给调用方。 */
export function writeMessageQueuePref(
  mode: MessageQueuePreference,
  storage: MessageQueuePrefStorage | null = getMessageQueuePrefStorage()
): void {
  if (!storage) return;
  try {
    storage.setItem(MESSAGE_QUEUE_PREF_STORAGE_KEY, mode);
  } catch (err) {
    console.warn('保存生成中发送偏好失败:', err);
  }
}

/**
 * `storage` 事件 → 需要的档位；与本键无关的事件返回 null（调用方据此忽略）。
 *
 * `event.key === null` 表示 `storage.clear()`：偏好被清掉，同样回落默认。
 */
export function messageQueuePrefFromStorageEvent(
  event: Pick<StorageEvent, 'key' | 'newValue'>
): MessageQueuePreference | null {
  if (event.key !== null && event.key !== MESSAGE_QUEUE_PREF_STORAGE_KEY) return null;
  return normalizeMessageQueuePref(event.newValue);
}

/** 档位元数据（文案与顺序）。图标映射留在组件层——`lib` 是纯 TS，不引 React 与图标库。 */
export const MESSAGE_QUEUE_PREF_OPTIONS = [
  {
    value: 'block' as const,
    label: '禁止',
    description: '回复生成期间锁定发送，草稿保留在输入框',
  },
  {
    value: 'insert' as const,
    label: '插入排队',
    description: '生成期间点发送将消息排入队列，本轮结束后依次发出',
  },
] as const;
