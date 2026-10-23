import { useCallback, useEffect, useMemo, useState } from 'react';
import type { PropsWithChildren } from 'react';

import { PerformanceUsageContext } from './performance-usage-context';
import {
  performanceUsageFromStorageEvent,
  readPerformanceUsage,
  writePerformanceUsage,
} from '../lib/performance-usage';
import type { PerformanceUsage } from '../lib/performance-usage';

/**
 * 「性能与用量」偏好的 provider。
 *
 * 与 `DetailLevelProvider` 同一套结构：初始值同步读 localStorage（读取自带 try/catch，
 * 隐私模式下回默认），写入随切换立即生效，并监听 `storage` 事件跟随其他标签页。
 *
 * 不用 `useEffect` 做首屏同步：初始 state 就是持久化值，第一帧即是对的——否则会先按
 * 默认档渲染一次统计药丸、再切一次，用户看到一次闪烁。
 */
export function PerformanceUsageProvider({ children }: PropsWithChildren) {
  const [mode, setModeState] = useState<PerformanceUsage>(() => readPerformanceUsage());

  const setMode = useCallback((next: PerformanceUsage) => {
    setModeState(next);
    writePerformanceUsage(next);
  }, []);

  // 多标签同步：storage 事件只在「其他标签页写同名键」时触发
  useEffect(() => {
    const handleStorage = (event: StorageEvent) => {
      const next = performanceUsageFromStorageEvent(event);
      if (next !== null) setModeState(next);
    };

    window.addEventListener('storage', handleStorage);
    return () => window.removeEventListener('storage', handleStorage);
  }, []);

  // memo 化 value：否则每次 provider 重渲染都会让所有消费者跟着重渲染
  const value = useMemo(() => ({ mode, setMode }), [mode, setMode]);

  return (
    <PerformanceUsageContext.Provider value={value}>{children}</PerformanceUsageContext.Provider>
  );
}
