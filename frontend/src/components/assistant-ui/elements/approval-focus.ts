/**
 * 审批卡片的播报文案与焦点接管判定。
 *
 * 抽成纯函数放在组件旁边，理由和 `lib/focus-trap.ts` 一样：前端测试跑在 node 环境、
 * 没有 React 渲染器也没有 DOM，而「什么时候该抢焦点、抢到哪里」正是最容易做错、又
 * 最该被钉住的一层——写成 effect 里的内联判断就只剩浏览器侧能验，改一行也没人发现。
 *
 * ## 为什么需要「首次出现」这层判定
 *
 * 卡片在生成过程中会随消息快照反复重渲染（流式增量、提交状态切换都会触发）。如果没有
 * 「这一批调用只接管一次」的闸门，用户点「拒绝」后的第一次重渲染就会把焦点从按钮上
 * 抢回来，键盘用户永远按不到第二条调用。判据用批次签名（`toolCallId` 拼接）而不是
 * 对象引用：同一批调用每次渲染都是新数组。
 */

/** 一批调用的稳定签名：只由 `toolCallId` 决定，与数组引用无关。 */
export function approvalBatchSignature(
  toolCalls: readonly { toolCallId: string }[],
): string {
  return toolCalls.map((call) => call.toolCallId).join('|');
}

/**
 * 这一批调用是否该由卡片接管焦点。
 *
 * @param focusedSignature 已经接管过焦点的那一批签名（`null` = 本次挂载还没接管过）
 * @param signature 当前这一批的签名
 */
export function shouldTakeApprovalFocus(
  focusedSignature: string | null,
  signature: string,
): boolean {
  // 空签名（没有调用）不该抢焦点：那种情况本来就不该渲染卡片
  if (!signature) return false;
  return focusedSignature !== signature;
}

/**
 * 焦点该落在哪里。
 *
 * **落在卡片容器上，而不是「批准」按钮上**——这一条与「接管焦点」的目的不冲突，
 * 但避开了自动聚焦最危险的那个后果。
 *
 * 要解决的问题只有两个：① 焦点留在输入框里，用户继续按 Enter 会发出**一条新的普通
 * 提问**，而待审批的这一轮被搁在一边；② 读屏用户对卡片的出现毫无感知。把焦点移到
 * 容器（`tabIndex={-1}`）同时解决两者：焦点离开 textarea 之后 Enter 不再提交输入框，
 * 容器带着 `aria-labelledby`/`aria-describedby` 被聚焦时读屏会念出标题与摘要。
 *
 * 而直接聚焦「批准」会把本文件顶部那条不变量（危险操作的默认值必须是不执行）破掉：
 * 卡片出现的瞬间用户可能手正按在回车键上，此时焦点若正落在「批准」上，一次误触就
 * 执行了一个没有 undo 的删除。用户从容器 Tab 一下才到按钮，这一步的代价远小于
 * 一次无法撤销的误操作。
 */
export type ApprovalFocusTarget = 'container' | 'none';

export function approvalFocusTarget(
  focusedSignature: string | null,
  signature: string,
): ApprovalFocusTarget {
  return shouldTakeApprovalFocus(focusedSignature, signature) ? 'container' : 'none';
}

/**
 * 给读屏的即时播报。
 *
 * 必须说清三件事，缺一件用户就无法做出正确的下一步：**有几条**（要不要当回事）、
 * **发生了什么**（有调用被拦下来了，不是模型答完了）、**怎么办**（批准后才会执行）。
 * 只说「需要审批」会让读屏用户不知道要按几次键。
 */
export function approvalAnnouncement(count: number): string {
  if (count <= 0) return '';
  if (count === 1) return '有 1 条工具调用需要你批准，批准后才会执行。';
  return `有 ${count} 条工具调用需要你批准，逐条表态后才会执行。`;
}
