/**
 * 运行过程的持久化（R-4 拆分之三）。
 *
 * 变化理由：存储结构。只认识 `db` 与 run 快照，不认识协议、不认识 UI。
 */

import { db } from '../db';
import type { MessageRecord, Run, SessionRecord } from '../types';

type MessageChanges = Partial<Omit<MessageRecord, 'id' | 'sessionId'>>;

/** run 里需要落库的字段。 */
function runFields(run: Run): MessageChanges {
  return {
    content: run.content,
    thinking: run.thinking,
    thinkingMs: run.thinkingMs,
    toolCalls: run.toolCalls,
  };
}

/** 流式过程中的节流落库：刷新或关页面时不至于丢掉整段回复。 */
export async function saveProgress(assistantMsgId: string, run: Run): Promise<void> {
  await db.messages.update(assistantMsgId, runFields(run));
}

export async function saveFinal(assistantMsgId: string, run: Run): Promise<void> {
  await db.messages.update(assistantMsgId, {
    ...runFields(run),
    stopped: run.stopped,
    incomplete: Boolean(run.incomplete),
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

export async function addMessages(records: MessageRecord[]): Promise<void> {
  await db.transaction('rw', db.messages, async () => {
    for (const record of records) await db.messages.add(record);
  });
}

export async function resetLastAssistantMessage(messageId: string): Promise<void> {
  await db.messages.update(messageId, {
    content: '',
    thinking: '',
    thinkingMs: 0,
    toolCalls: [],
    stopped: false,
    clarification: null,
  });
}

export async function updateSession(
  sessionId: string,
  changes: Partial<Omit<SessionRecord, 'id'>>,
): Promise<void> {
  await db.sessions.update(sessionId, changes);
}

/**
 * 读取送往模型的历史上下文。
 *
 * 两点刻意为之：
 * 1. 排除本轮占位消息，并丢掉内容为空的助手消息——那些是流式占位符，进上下文只会污染输入。
 * 2. **不回放 `thinking`**。思考链只用于展示：把它回传会白白放大每次请求的体积，
 *    而且长度不可控——一旦超过后端的 `MAX_THINKING_CHARS`，该会话的每一次重试都会
 *    拿到 422，用户被永久卡住。
 */
export async function loadContextMessages(
  sessionId: string,
  { excludeId, limit }: { excludeId: string; limit: number },
): Promise<Array<{ role: string; content: string }>> {
  const all = await loadSessionMessages(sessionId);

  return all
    .filter((message) => message.id !== excludeId)
    .filter((message) => message.role !== 'assistant' || (message.content && message.content.trim()))
    .slice(-limit)
    .map((message) => ({ role: message.role, content: message.content }));
}
