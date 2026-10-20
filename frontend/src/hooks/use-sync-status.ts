import { useSyncExternalStore } from 'react';

import { getStatus, subscribeStatus, type SyncStatus } from '@/lib/sync/engine';

/**
 * 同步状态（只读快照）。
 *
 * 用 `useSyncExternalStore` 而不是 `useState + useEffect`：引擎是 React 之外的
 * 单例，这是 React 官方给「外部 store」准备的接口——订阅与快照由它统一处理，
 * 既不会出现「effect 里 setState 触发级联渲染」，也不会在订阅建立与首次渲染之间
 * 读到过期值。
 */
export function useSyncStatus(): SyncStatus {
  return useSyncExternalStore(subscribeStatus, getStatus, getStatus);
}
