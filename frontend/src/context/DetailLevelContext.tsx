import { useCallback, useEffect, useMemo, useState } from 'react';
import type { PropsWithChildren } from 'react';

import { DetailLevelContext } from './detail-level-context';
import {
  collapseTimingFromStorageEvent,
  detailLevelFromStorageEvent,
  presentationPolicyFor,
  readCollapseTiming,
  readDetailLevel,
  writeCollapseTiming,
  writeDetailLevel,
} from '../lib/detail-level';
import type { CollapseTiming, DetailLevel } from '../lib/detail-level';

/**
 * 「对话显示」偏好的 provider。
 *
 * 与 `ThemeProvider` 同一套结构：初始值同步读 localStorage（读取自带 try/catch），
 * 写入随切换立即生效，并监听 `storage` 事件跟随其他标签页的切换。
 *
 * 不用 `useEffect` 做首屏同步：初始 state 就是持久化值，渲染第一帧即是对的——否则
 * 会先按默认档渲染一次、再切一次，用户看到一次闪烁。
 *
 * 两个偏好（四档 + 收起时机）是**两个独立 state、两个存储键**：它们正交，改一个
 * 不该重写另一个，也不该在 storage 事件里互相干扰。
 */
export function DetailLevelProvider({ children }: PropsWithChildren) {
  const [level, setLevelState] = useState<DetailLevel>(() => readDetailLevel());
  const [collapseTiming, setCollapseTimingState] = useState<CollapseTiming>(() =>
    readCollapseTiming(),
  );

  const setLevel = useCallback((next: DetailLevel) => {
    setLevelState(next);
    writeDetailLevel(next);
  }, []);

  const setCollapseTiming = useCallback((next: CollapseTiming) => {
    setCollapseTimingState(next);
    writeCollapseTiming(next);
  }, []);

  // 多标签同步：storage 事件只在「其他标签页写同名键」时触发
  useEffect(() => {
    const handleStorage = (event: StorageEvent) => {
      const nextLevel = detailLevelFromStorageEvent(event);
      if (nextLevel !== null) setLevelState(nextLevel);
      const nextTiming = collapseTimingFromStorageEvent(event);
      if (nextTiming !== null) setCollapseTimingState(nextTiming);
    };

    window.addEventListener('storage', handleStorage);
    return () => window.removeEventListener('storage', handleStorage);
  }, []);

  // memo 化 value：否则每次 provider 重渲染都会让所有消费者跟着重渲染。
  // policy 也在这里算：它对同一对入参是同一个引用，放进 value 不会破坏 memo。
  const value = useMemo(
    () => ({
      level,
      setLevel,
      collapseTiming,
      setCollapseTiming,
      policy: presentationPolicyFor(level, collapseTiming),
    }),
    [level, setLevel, collapseTiming, setCollapseTiming],
  );

  return <DetailLevelContext.Provider value={value}>{children}</DetailLevelContext.Provider>;
}
