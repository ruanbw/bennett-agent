/**
 * 当前会话的上下文（与 `clarification-context.ts` 同一种形态：context 与 hook 同文件）。
 *
 * 值由 `ChatProvider` 提供；消费者目前有两类：
 *   - 会话级提示条（`ChatSessionNotices`，需要 sessionId 去查 run 状态）；
 *   - 输入框草稿（按会话保存，需要知道「现在是在哪个会话里打字」）。
 */

import { createContext, useContext } from 'react';

export interface ChatSessionContextValue {
  sessionId: string;
  /** 一次性的用户可见提示（发送未成功等）；null 表示没有。 */
  notice: string | null;
  dismissNotice: () => void;
}

export const ChatSessionContext = createContext<ChatSessionContextValue | null>(null);

/**
 * 当前会话 id；不在 ChatProvider 里（理论上不该发生）时返回 null。
 *
 * 返回 null 而不是抛错：调用方（输入框草稿）拿不到 id 只是「不保存草稿」，
 * 不该让整个输入框崩掉。
 */
export function useChatSessionId(): string | null {
  return useContext(ChatSessionContext)?.sessionId ?? null;
}
