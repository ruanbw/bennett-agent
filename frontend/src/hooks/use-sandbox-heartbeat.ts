/**
 * 会话页面可见时给沙箱续命。
 *
 * 机器是服务端按 `max(心跳, 最近活动)` 判定是否空闲的：没有心跳、也没有工具调用，10 分钟
 * 后就会被回收。而心跳的唯一来源就是这里，所以两条规矩不能少：
 *   - **只在可见时发**：被用户忘在后台的标签页不该无限期占住一台机器；
 *   - **回到可见立刻补一次**：只靠定时器的话，用户切回标签页要先干等一个周期才续命，
 *     而短时间反复切页甚至可能一次都等不到。
 */

import { useEffect } from 'react';

import { SANDBOX_HEARTBEAT_INTERVAL_MS, sendSandboxHeartbeat } from '../lib/sandbox';
import { isSessionIdSendable } from '../lib/session-id';

/**
 * 开始心跳，返回停表函数。
 *
 * 抽成普通函数而不是全塞进 `useEffect`：前端测试跑在 node 环境、没有 React 渲染器，而
 * 「可见才发 / 切回立刻发 / 停表后不再发」正是要守住的契约（与 `use-media-query` 把
 * store 抽出来测是同一个理由）。hook 只负责在会话切换与卸载时调用它。
 */
export function startSandboxHeartbeat(sessionId: string): () => void {
  // 发不出去的 id（空值、或形状不符会让服务端 422）连定时器都不该起：
  // 否则每 30s 都是一次注定失败的请求，而这个会话本来就没有机器可续
  if (!isSessionIdSendable(sessionId)) return () => {};
  // SSR / node 环境没有 document：没有页面可见性可言，也就不发心跳
  if (typeof document === 'undefined') return () => {};

  let timer: ReturnType<typeof setInterval> | null = null;

  const beat = (): void => {
    // 刻意不 await：心跳的成功与否没有任何后续动作，失败已在 sendSandboxHeartbeat 里吞掉
    void sendSandboxHeartbeat(sessionId);
  };

  const stopTimer = (): void => {
    if (timer !== null) clearInterval(timer);
    timer = null;
  };

  /**
   * 把定时器对齐到当前可见性。
   *
   * 隐藏时直接停表而不是「留着定时器但每次跳过」：后台标签页的定时器本来就会被浏览器
   * 节流，留一个永远不干活的计时器只是让「为什么不发心跳」更难查。
   */
  const sync = (): void => {
    if (document.visibilityState !== 'visible') {
      stopTimer();
      return;
    }
    beat();
    if (timer === null) timer = setInterval(beat, SANDBOX_HEARTBEAT_INTERVAL_MS);
  };

  // 挂载即对齐：会话打开的那一刻就算「用户在看着」，不必等满一个周期才第一次续命
  sync();
  document.addEventListener('visibilitychange', sync);

  return () => {
    stopTimer();
    document.removeEventListener('visibilitychange', sync);
  };
}

/** 当前激活会话的心跳。`sessionId` 为 null（还没选中会话）时不发任何请求。 */
export function useSandboxHeartbeat(sessionId: string | null): void {
  useEffect(() => {
    if (!sessionId) return undefined;
    // 会话切换/组件卸载都走这条清理：上一个会话的定时器不能继续给旧机器续命
    return startSandboxHeartbeat(sessionId);
  }, [sessionId]);
}
