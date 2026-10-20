/**
 * 设置面板打开时读一次本机数据统计。
 *
 * 只在打开时读：统计数据不是实时指标，关闭期间没必要扫库。`open` 变 true 才发查询，
 * 关闭后回来的结果直接丢弃（面板可能已经关掉）。
 */

import { useEffect, useState } from 'react';

import { readLocalDataStats, type LocalDataStats } from '../lib/local-stats';

export function useLocalDataStats(open: boolean): LocalDataStats | null {
  const [stats, setStats] = useState<LocalDataStats | null>(null);

  useEffect(() => {
    if (!open) return undefined;

    let active = true;
    void readLocalDataStats()
      .then((next) => {
        if (active) setStats(next);
      })
      .catch((err) => {
        // 统计读不出来不影响设置里的其他功能：区块会显示占位符
        console.error('读取本机数据统计失败:', err);
      });

    return () => {
      active = false;
    };
  }, [open]);

  return stats;
}
