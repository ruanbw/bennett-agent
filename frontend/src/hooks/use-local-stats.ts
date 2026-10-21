/**
 * 设置面板打开时读一次本机数据统计。
 *
 * 只在打开时读：统计数据不是实时指标，关闭期间没必要扫库。`open` 变 true 才发查询，
 * 关闭后回来的结果直接丢弃（面板可能已经关掉）。
 *
 * **失败要说出来**：此前这里只 `console.error`，面板上三个数字停在「—」。对一个「数据
 * 默认只存在本机」的应用来说，这是最容易被误读的状态——用户会以为自己的会话丢了。
 * 现在把失败也交给界面，并给一个重试入口。
 */

import { useCallback, useEffect, useState } from 'react';

import { readLocalDataStats, type LocalDataStats } from '../lib/local-stats';

export interface LocalDataStatsState {
  /** 读到的统计数据；未读完 / 读失败时为 null。 */
  stats: LocalDataStats | null;
  /** 已失败（面板据此显示原因与重试，而不是一直显示占位符）。 */
  failed: boolean;
  /** 再读一次。 */
  retry: () => void;
}

export function useLocalDataStats(open: boolean): LocalDataStatsState {
  const [stats, setStats] = useState<LocalDataStats | null>(null);
  const [failed, setFailed] = useState(false);
  /** 重试令牌：变化即重新查询（不额外引入「请求中」状态，占位符已经表达了它）。 */
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!open) return undefined;

    let active = true;
    void readLocalDataStats()
      .then((next) => {
        if (!active) return;
        setStats(next);
        setFailed(false);
      })
      .catch((err) => {
        // 统计读不出来不影响设置里的其他功能，但要让用户知道「是读不出来」而不是「是空的」
        console.error('读取本机数据统计失败:', err);
        if (active) setFailed(true);
      });

    return () => {
      active = false;
    };
  }, [open, attempt]);

  const retry = useCallback(() => {
    setFailed(false);
    setAttempt((value) => value + 1);
  }, []);

  return { stats, failed, retry };
}
