/**
 * 发送按钮的可发送判定与提示文案。
 *
 * 抽成纯函数的理由：三个禁用理由的优先级会直接影响用户看到什么——上下文满了
 * 却提示「附件未开放」等于指错方向；空输入时按钮仍可点则像坏了。组件只负责读数。
 *
 * ## 为什么附件文案不提「后端」
 *
 * 后端拒绝非文本消息是**正确的协议边界**，不是缺陷；但把原因说成「后端只接受文本」
 * 会让用户以为部署出了问题、去催运维。真实情况是这个产品本身还没开放附件上传。
 * 文案只陈述「暂未开放」（与 `lib/attachment-labels.ts` 的
 * `attachmentUnsupportedMessage` 保持一致），不暗示任何一方坏了。
 */

export interface SendGateInput {
  /** 本会话上下文已满（服务端已经会拒绝，这里只是不让人白点）。 */
  contextLocked: boolean;
  /** 贴了/选了附件，但当前版本不接受附件。 */
  hasAttachments: boolean;
  /** 输入框没有有效内容（只有空白也算空）。 */
  isEmpty: boolean;
}

export interface SendGate {
  disabled: boolean;
  /** 同时用作按钮的 tooltip 与 aria-label。 */
  tooltip: string;
}

export function resolveSendGate({ contextLocked, hasAttachments, isEmpty }: SendGateInput): SendGate {
  // 先解释「为什么不能继续」，再解释临时性原因；附件可以移除，上下文锁不能。
  if (contextLocked) return { disabled: true, tooltip: '本会话上下文已满，请新建会话' };
  if (hasAttachments) return { disabled: true, tooltip: '本项目暂未开放附件上传，请先移除' };
  if (isEmpty) return { disabled: true, tooltip: '输入内容后发送' };
  return { disabled: false, tooltip: '发送消息' };
}
