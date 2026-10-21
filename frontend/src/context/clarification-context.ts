import { createContext, useContext } from 'react';

import type { PendingClarification } from '../lib/chat-message';

/**
 * 意图确认提问的上下文。
 *
 * 取代原来的 `React.cloneElement(children, {...})`：ChatProvider 会把三个
 * 「提问相关」的 prop 注入到任意子元素上，再由 ChatContainer 原样转发给 Thread。
 * 那是一层看不见的耦合——ChatContainer 的 props 里看不出它们从哪来，
 * 而且只有 Thread 真正用到。
 *
 * 与 `theme-context.ts` 一致：context 与 hook 同文件，不再拆出一个纯转发的
 * `use-xxx.js`（纯转发的中间层没有价值）。
 */

export interface ClarificationContextValue {
  /** 当前待答提问（含 messageId），无则 null */
  pending: PendingClarification | null;
  /**
   * 用户选定或输入后提交：把答案作为新一轮用户消息发出。
   *
   * 返回 Promise 而不是 void：发送可能失败（消息写库失败、run 槽位被抢），弹层要据此
   * 解锁并让用户重试——否则用户点完选项，弹层变灰卡在那里，既没发出去也没法再点。
   */
  answer: (answerText: string) => void | Promise<unknown>;
  /** 用户忽略提问 */
  dismiss: () => void;
}

export const ClarificationContext = createContext<ClarificationContextValue>({
  pending: null,
  answer: () => {},
  dismiss: () => {},
});

export const ClarificationProvider = ClarificationContext.Provider;

export function useClarification(): ClarificationContextValue {
  return useContext(ClarificationContext);
}
