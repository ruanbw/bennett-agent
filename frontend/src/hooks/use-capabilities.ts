/**
 * 启动时拉一次运行时能力清单。
 *
 * 能力只用于裁剪起步建议与设置里的说明，失败**不阻塞**界面：缺省值（无联网搜索）
 * 本身就是安全的一侧；重试逻辑的首要对象仍是真正影响对话的 `/api/models`。
 *
 * 但「一次失败 = 永久未知」是不对的：启动瞬间正是最容易失败的时机（会话 cookie 刚
 * 生效、反代在建连、标签页冷启动）。所以这里做两件事：
 *
 * 1. 失败后**自动隔一会儿再试一次**——启动竞态通常几秒内就消失，用户不该为此刷新页面；
 * 2. 暴露 `retry`，让设置面板的「未知」旁边有一个可点的「重新探测」，以及模型菜单的
 *    「重试」顺带把它一起刷新（管理员刚配好 Key 时最常用的动作就是这个）。
 */

import { useCallback, useEffect, useState } from 'react';

import { fetchCapabilities } from '../lib/capabilities';
import type { Capabilities } from '../types';

/** 首次失败后自动重试的等待时间：够躲过启动竞态，又不至于让用户等太久。 */
export const CAPABILITIES_AUTO_RETRY_MS = 3_000;

export interface CapabilitiesState {
  /** `null` 表示「还没拿到 / 拿不到」——设置面板要区分「未启用」与「未知」。 */
  capabilities: Capabilities | null;
  /** 最近一次探测失败（拿到结果后清空）。界面据此给出「重新探测」入口。 */
  failed: boolean;
  /** 再探测一次。 */
  retry: () => void;
}

export function useCapabilities(): CapabilitiesState {
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [failed, setFailed] = useState(false);
  /** 重试令牌：变化即重新探测（自动重试与手动重试共用）。 */
  const [attempt, setAttempt] = useState(0);
  /** 是否已经自动重试过：只自动补一次，之后交给用户或下一次刷新。 */
  const [autoRetried, setAutoRetried] = useState(false);

  useEffect(() => {
    let active = true;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;

    void fetchCapabilities()
      .then((next) => {
        if (!active) return;
        setCapabilities(next);
        setFailed(false);
      })
      .catch((err) => {
        // 装饰性数据失败只留一行日志；控制台之外不打扰用户
        console.error('获取运行时能力失败:', err);
        if (!active) return;
        setFailed(true);
        if (!autoRetried) {
          retryTimer = setTimeout(() => {
            setAutoRetried(true);
            setAttempt((value) => value + 1);
          }, CAPABILITIES_AUTO_RETRY_MS);
        }
      });

    return () => {
      active = false;
      if (retryTimer !== null) clearTimeout(retryTimer);
    };
  }, [attempt, autoRetried]);

  const retry = useCallback(() => {
    setFailed(false);
    setAttempt((value) => value + 1);
  }, []);

  return { capabilities, failed, retry };
}
