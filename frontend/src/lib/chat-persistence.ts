/**
 * 运行过程的持久化（R-4 拆分之三）。
 *
 * 变化理由：存储结构。只认识 `db` 与 run 快照，不认识协议、不认识 UI。
 *
 * 已知限制：多标签页打开同一会话时没有跨页互斥，两边各自落库、后写的覆盖先写的
 * （IndexedDB 不提供跨页事务协调；详见 chat-store.ts 顶部说明）。
 */

import { db } from '../db';
import { isRunIncomplete, type RunState } from './chat-store';
import type { ClarificationState, MessageRecord, Run } from '../types';

type MessageChanges = Partial<Omit<MessageRecord, 'id' | 'sessionId'>>;

/** run 里需要落库的字段。 */
function runFields(run: Run): MessageChanges {
  return {
    content: run.content,
    thinking: run.thinking,
    thinkingMs: run.thinkingMs,
    toolCalls: run.toolCalls,
    // 本轮请求的模型：上下文是否已满按模型判定，锁定状态靠它区分
    model: run.model ?? null,
    // 事件到达顺序：没有它，刷新后思考/工具/正文只能按默认顺序近似还原
    partOrder: run.partOrder ?? [],
    // 备用模型信息：刷新后提示行仍然要显示，否则用户会以为还是主模型答的
    fallback: run.fallback ?? null,
    // 真实用量：刷新后「上下文用了多少」依旧可见（它也是估算器下一轮的锚点）
    usage: run.usage ?? null,
    // 失败信息：刷新后「重试」按钮仍然可用（是否可重试由服务端判定并持久化）
    error: run.error ?? null,
  };
}

/**
 * 流式过程中的节流落库：刷新或关页面时不至于丢掉整段回复。
 *
 * 落库的内容一定标 `incomplete: true`：这是「半截」的持久化，页面刷新后必须能
 * 看出它没跑完。终态落库（saveFinal）会按真实终止原因把它改回准确值。
 */
export async function saveProgress(assistantMsgId: string, run: Run): Promise<void> {
  await db.messages.update(assistantMsgId, { ...runFields(run), incomplete: true });
}

/**
 * 终态落库。
 *
 * `stopped` / `incomplete` 都取 run 上的结算值（与 finishRun 共用 `isRunIncomplete`）：
 * 只有真的不完整才写 true；干净结束必须把 saveProgress 写下的 `incomplete: true`
 * 覆盖回 false，否则刷新后一条正常回复也会被标成「可能不完整」。
 */
export async function saveFinal(assistantMsgId: string, run: RunState): Promise<void> {
  await db.messages.update(assistantMsgId, {
    ...runFields(run),
    stopped: run.stopped,
    incomplete: isRunIncomplete(run),
    clarification: run.clarification ?? null,
  });
}

export async function touchSession(sessionId: string): Promise<void> {
  await db.sessions.update(sessionId, { updatedAt: Date.now() });
}

/**
 * 清掉助手消息上的待答提问。
 *
 * 这就是「用户忽略了提问」的全部语义：记录上不再带 `clarification`，
 * 于是 `pendingClarification` 的推导条件自然不成立。不需要额外的 `handled` 标记。
 */
export async function clearMessageClarification(messageId: string): Promise<void> {
  await db.messages.update(messageId, { clarification: null });
}

export async function loadSessionMessages(sessionId: string): Promise<MessageRecord[]> {
  return db.messages.where('sessionId').equals(sessionId).sortBy('createdAt');
}

export async function countSessionMessages(sessionId: string): Promise<number> {
  return db.messages.where('sessionId').equals(sessionId).count();
}

// ---------------------------------------------------------------------
// 上下文回放
// ---------------------------------------------------------------------
/** 送往模型的历史消息。与 `chat-transport` 的请求形状一致：只有 role 与 content。 */
export interface ContextMessage {
  role: string;
  content: string;
}

/**
 * 回传的真实用量锚点：`messageIndex` 指的是**送达模型的那个数组**里的位置。
 *
 * 下标必须对应 `messages` 而不是本地记录：中间还可能被 `clampContextMessages` 裁过、
 * 空白占位消息也被滤过，位置早就不是记录里的了。
 */
export interface ContextAnchor {
  messageIndex: number;
  contextTokens: number;
}

/**
 * 回放用的消息：比协议多一个「这条消息产生的那轮真实用量」。
 *
 * 它只在本地推导锚点时有用，发请求前会被剥掉（`loadContextMessages` 的返回把消息
 * 压回 `{role, content}`）——协议里多一个字段就多一处两边要对齐的地方。
 */
interface ReplayableMessage extends ContextMessage {
  anchorTokens?: number;
}

/**
 * 单条 content 的前端上限。后端是 32_000（`app/schemas/chat.py:MAX_MESSAGE_CHARS`），
 * 这里留 2000 字符余量：同一份内容在两侧的计数口径将来可能微调，贴边等于随时 422。
 */
export const CONTEXT_MAX_MESSAGE_CHARS = 30_000;

/**
 * 历史总量的前端上限。后端是 400_000（`MAX_HISTORY_CHARS`，按 content+thinking 计），
 * 这里同样留余量。前端不回放 thinking，所以实际统计的就是各条 content 之和。
 */
export const CONTEXT_MAX_TOTAL_CHARS = 380_000;

export interface ClampContextOptions {
  maxContentChars?: number;
  maxTotalChars?: number;
}

function totalChars(messages: readonly ContextMessage[]): number {
  let total = 0;
  for (const message of messages) total += message.content.length;
  return total;
}

/**
 * 把历史压进后端的硬上限，避免「一条超长回复让该会话每次请求都 422」的永久卡死。
 *
 * 策略固定为「先截单条、再丢最旧、必要时截断最长」：
 *   1. 单条超上限就截断——超长的那一条只要还在，每一轮都会被后端拒掉；
 *   2. 总量超上限就丢最旧——越旧的消息对当前轮次越不重要；
 *   3. 只剩一条仍超总量（单条上限比总预算还大）时才截断最长的一条。
 *
 * 纯函数：不改入参，未超限时原样返回内容（不做任何无谓改写）。
 */
export function clampContextMessages<T extends ContextMessage>(
  messages: readonly T[],
  {
    maxContentChars = CONTEXT_MAX_MESSAGE_CHARS,
    maxTotalChars = CONTEXT_MAX_TOTAL_CHARS,
  }: ClampContextOptions = {},
): T[] {
  const withinPerMessage = messages.every((message) => message.content.length <= maxContentChars);
  if (withinPerMessage && totalChars(messages) <= maxTotalChars) return [...messages];

  // `as T`：这里只改 content，其余字段（含锚点这类本地附加字段）原样带过去。
  // 泛型无法表达「浅拷贝并覆盖某字段仍是 T」，但这是本函数唯一的改动方式。
  const clamped = messages.map((message) =>
    (message.content.length > maxContentChars
      ? { ...message, content: message.content.slice(0, maxContentChars) }
      : { ...message }) as T,
  );

  while (clamped.length > 1 && totalChars(clamped) > maxTotalChars) clamped.shift();

  const total = totalChars(clamped);
  if (total > maxTotalChars && clamped.length > 0) {
    let longest = 0;
    for (let i = 1; i < clamped.length; i += 1) {
      if (clamped[i].content.length > clamped[longest].content.length) longest = i;
    }
    const others = total - clamped[longest].content.length;
    const budget = Math.max(0, maxTotalChars - others);
    clamped[longest] = { ...clamped[longest], content: clamped[longest].content.slice(0, budget) } as T;
  }

  return clamped;
}

/**
 * 把带 clarification 的助手轮合成为可回放的文本。
 *
 * 提问只存在消息的 `clarification` 字段里，`content` 往往是空的。此前整条被
 * 「content 为空的助手消息」过滤掉，用户的回答就成了孤立 user 消息——模型看不到
 * 自己刚问的问题，答非所问。
 */
function renderClarification(clarification: ClarificationState): string {
  const lines = [clarification.question];
  if (clarification.options.length > 0) {
    lines.push('', '可选项：');
    clarification.options.forEach((option, index) => lines.push(`${index + 1}. ${option}`));
  }
  lines.push('', '（用户下一条消息是对该问题的回答。）');
  return lines.join('\n');
}

function toReplayable(message: MessageRecord): ReplayableMessage {
  const content =
    message.role === 'assistant' && !message.content.trim() && message.clarification
      ? renderClarification(message.clarification)
      : message.content;

  // 只有助手消息能带用量：它是那一轮回答产生的那次请求的 prompt token 数
  const anchorTokens =
    message.role === 'assistant' && typeof message.usage?.contextTokens === 'number'
      ? message.usage.contextTokens
      : undefined;

  return { role: message.role, content, anchorTokens };
}

/**
 * 读取送往模型的历史上下文。
 *
 * 三点刻意为之：
 * 1. 排除本轮占位消息，并丢掉内容为空的助手消息——那些是流式占位符，进上下文只会污染输入。
 *    例外是带 clarification 的空助手消息：提问藏在 `clarification` 字段里，必须合成
 *    成文本回放（见 renderClarification）。
 * 2. **不回放 `thinking`**。思考链只用于展示：把它回传会白白放大每次请求的体积，
 *    而且长度不可控——一旦超过后端的 `MAX_THINKING_CHARS`，该会话的每一次重试都会
 *    拿到 422，用户被永久卡住。
 * 3. 最后统一过一遍 clampContextMessages：条数裁剪挡不住单条超长与总量超长。
 */
export async function loadContextMessages(
  sessionId: string,
  { excludeId, limit }: { excludeId: string; limit: number },
): Promise<{ messages: ContextMessage[]; anchor: ContextAnchor | null }> {
  const all = await loadSessionMessages(sessionId);

  const replayable = all
    .filter((message) => message.id !== excludeId)
    .filter(
      (message) => message.role !== 'assistant' || message.content.trim() || message.clarification,
    )
    .slice(-limit)
    .map(toReplayable);

  const clamped = clampContextMessages(replayable);

  // 锚点在**裁剪之后**算：下标要对应真正发出去的那个数组。
  // 从这里往回找最后一条带真实用量的助手消息——它的 `contextTokens` 覆盖了它之前的
  // 全部内容（含系统提示词），所以预检只需再估算它之后的那几条。
  let anchor: ContextAnchor | null = null;
  for (let index = clamped.length - 1; index >= 0; index -= 1) {
    const tokens = clamped[index].anchorTokens;
    if (typeof tokens === 'number' && tokens > 0) {
      anchor = { messageIndex: index, contextTokens: tokens };
      break;
    }
  }

  // 只把协议字段交出去：`anchorTokens` 是本地推导用的，不跟着发上游
  return { messages: clamped.map(({ role, content }) => ({ role, content })), anchor };
}
