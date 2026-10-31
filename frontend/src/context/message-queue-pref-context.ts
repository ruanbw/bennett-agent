/**
 * 「生成中发送」偏好的上下文。
 *
 * 与 `performance-usage-context.ts` 同一形态：纯逻辑（白名单、存储读写、storage 事件
 * 解析）在 `lib/message-queue-pref.ts`，这里只负责把值递给组件树。
 *
 * 为什么需要 provider 而不是让消费方各自读 localStorage：设置页是唯一写入入口，而
 * 消费方（`ChatProvider` 的队列装配、输入框的发送门）与设置页互不相邻。各自读
 * localStorage 会得到两个互不通知的副本——改完设置要么刷新页面、要么等下一次
 * storage 事件（同标签页内根本不会触发）。一个 provider 让「改」与「看」共享同一份
 * 状态。
 *
 * 消费者：
 *   - `ChatProvider`：决定要不要给 runtime 装配队列适配器；
 *   - 输入框（`thread.aui.tsx`）：生成期间发送按钮的文案与禁用理由。
 */

import { createContext, useContext } from 'react';

import {
  DEFAULT_MESSAGE_QUEUE_PREF,
  type MessageQueuePreference,
} from '../lib/message-queue-pref';

export interface MessageQueuePrefContextValue {
  mode: MessageQueuePreference;
  setMode: (mode: MessageQueuePreference) => void;
}

export const MessageQueuePrefContext = createContext<MessageQueuePrefContextValue>({
  mode: DEFAULT_MESSAGE_QUEUE_PREF,
  setMode: () => {},
});

export function useMessageQueuePref(): MessageQueuePrefContextValue {
  return useContext(MessageQueuePrefContext);
}
