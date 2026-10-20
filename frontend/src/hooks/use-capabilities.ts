/**
 * 启动时拉一次运行时能力清单。
 *
 * 失败不重试也不阻塞界面：能力只用于裁剪起步建议，缺省值（无联网搜索）本身就是
 * 安全的一侧；重试逻辑留给真正影响对话的 `/api/models`。
 */

import { useEffect, useState } from 'react';

import { UNKNOWN_CAPABILITIES, fetchCapabilities } from '../lib/capabilities';
import type { Capabilities } from '../types';

export function useCapabilities(): Capabilities {
  const [capabilities, setCapabilities] = useState<Capabilities>(UNKNOWN_CAPABILITIES);

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
