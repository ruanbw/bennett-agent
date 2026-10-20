/**
 * 会在本地午夜自动前进的「现在」。
 *
 * 会话分组标签依赖「今天是哪一天」（今天 / 昨天 / 7 天内）。标签页挂机过午夜后，
 * `useMemo` 的依赖没有变化，昨天创建的会话会一直挂在「今天」下面，直到用户发消息
 * 触发 sessions 变化。这里在下一个本地午夜把 `now` 推一格；标签页从后台恢复时也刷新
 * （设备睡眠可能错过定时器，浏览器也会节流后台标签页的定时器）。
 *
 * 成本只有一次重渲染：分组是纯计算，几十条会话的排序可以忽略。
 */

import { useEffect, useState } from 'react';

import { msUntilNextLocalMidnight } from '../lib/session-grouping';

export function useMidnightRefresh(): number {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    let timer: number | undefined;

    const schedule = () => {
      if (timer !== undefined) window.clearTimeout(timer);
      timer = window.setTimeout(() => {
        setNow(Date.now());
        schedule();
      }, msUntilNextLocalMidnight());
    };

    const handleVisibility = () => {
      if (document.visibilityState !== 'visible') return;
      // 睡眠/后台期间定时器可能被跳过或节流：回来时先对齐一次，再按当前时间重排
      setNow(Date.now());
      schedule();
    };

    schedule();
    document.addEventListener('visibilitychange', handleVisibility);
    return () => {
      if (timer !== undefined) window.clearTimeout(timer);
      document.removeEventListener('visibilitychange', handleVisibility);
    };
  }, []);

  return now;
}
