import { createContext, useContext } from 'react';

import type { PendingApproval } from '../lib/chat-message';

/**
 * 工具审批（HITL）的上下文。
 *
 * 与 `clarification-context.ts` 完全对称，但两者**不能合并**：它们的提交动作语义不同。
 * 澄清是「把用户的回答当成新一轮提问发出去」，审批是「重跑同一条助手消息并把决定带进
 * 请求」——后者不产生新的用户消息。合并成一个「待答」context 会让展示层被迫按 `kind`
 * 分支处理，而那正是这层抽象要避免的东西。
 */

export interface ApprovalContextValue {
  /** 当前待审批的调用（含 messageId），无则 null */
  pending: PendingApproval | null;
  /**
   * 用户做出决定后提交：`tool_call_id` → 是否批准。
   *
   * 返回 Promise<boolean>：`false` 表示没能起跑（槽位被占/没有决定），卡片据此解锁并
   * 让用户重试——否则点完按钮卡片会变灰卡在那里，既没发出去也没法再点。
   */
  submit: (decisions: Record<string, boolean>) => Promise<boolean>;
  /** 用户不打算处理这些调用（关掉卡片）：清掉标记，本轮就此作废 */
  dismiss: () => void;
}

export const ApprovalContext = createContext<ApprovalContextValue>({
  pending: null,
  submit: async () => false,
  dismiss: () => {},
});

export const ApprovalProvider = ApprovalContext.Provider;

export function useApproval(): ApprovalContextValue {
  return useContext(ApprovalContext);
}
