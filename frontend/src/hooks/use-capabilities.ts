/**
 * 启动时拉一次运行时能力清单。
 *
 * 失败不重试也不阻塞界面：能力只用于裁剪起步建议，缺省值（无联网搜索）本身就是
 * 安全的一侧；重试逻辑留给真正影响对话的 `/api/models`。
 */

import { useEffect, useState } from 'react';

import { fetchCapabilities } from '../lib/capabilities';
import type { Capabilities } from '../types';

/**
 * 返回 `null` 表示「还没拿到 / 拿不到」。
 *
 * 不用「无联网搜索」的假数据顶替：设置面板要区分「未启用」与「未知」，拿保守值当
 * 结论会把「探测失败」说成「服务端没配」。调用方按 `?? false` 降级到保守行为即可。
 */
export function useCapabilities(): Capabilities | null {
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);

  useEffect(() => {
    let active = true;

    void fetchCapabilities()
      .then((next) => {
        if (active) setCapabilities(next);
      })
      .catch((err) => {
        // 装饰性数据失败只留一行日志；控制台之外不打扰用户
        console.error('获取运行时能力失败:', err);
      });

    return () => {
      active = false;
    };
  }, []);

  return capabilities;
}
