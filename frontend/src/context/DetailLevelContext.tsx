import { useCallback, useEffect, useMemo, useState } from 'react';
import type { PropsWithChildren } from 'react';

import { DetailLevelContext } from './detail-level-context';
import {
  detailLevelFromStorageEvent,
  readDetailLevel,
  writeDetailLevel,
} from '../lib/detail-level';
import type { DetailLevel } from '../lib/detail-level';

/**
 * 「对话显示」偏好的 provider。
 *
 * 与 `ThemeProvider` 同一套结构：初始值同步读 localStorage（读取自带 try/catch），
 * 写入随切换立即生效，并监听 `storage` 事件跟随其他标签页的切换。
 *
 * 不用 `useEffect` 做首屏同步：初始 state 就是持久化值，渲染第一帧即是对的——否则
 * 会先按默认档渲染一次、再切一次，用户看到一次闪烁。
 */
export function DetailLevelProvider({ children }: PropsWithChildren) {
  const [level, setLevelState] = useState<DetailLevel>(() => readDetailLevel());

  const setLevel = useCallback((next: DetailLevel) => {
    setLevelState(next);
    writeDetailLevel(next);
  }, []);

  // 多标签同步：storage 事件只在「其他标签页写同名键」时触发
  useEffect(() => {
    const handleStorage = (event: StorageEvent) => {
      const next = detailLevelFromStorageEvent(event);
      if (next !== null) setLevelState(next);
    };

    window.addEventListener('storage', handleStorage);
    return () => window.removeEventListener('storage', handleStorage);
  }, []);

  // memo 化 value：否则每次 provider 重渲染都会让所有消费者跟着重渲染
  const value = useMemo(() => ({ level, setLevel }), [level, setLevel]);

  return <DetailLevelContext.Provider value={value}>{children}</DetailLevelContext.Provider>;
}
