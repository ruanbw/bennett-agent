import { describe, expect, it } from 'vitest';

import type { MessageRecord, SessionRecord } from '../../types';
import {
  buildPushBatch,
  fromConversationRow,
  fromMessageRow,
  shouldApplyRemote,
  toConversationPushRow,
  toMessagePushRow,
  type ConversationRow,
  type MessageRow,
} from './mapping';

const session = (overrides: Partial<SessionRecord> = {}): SessionRecord => ({
  id: 'sess_a',
  title: '标题',
  model: 'auto/x',
  reasoningEffort: 'medium',
  isPinned: 0,
  createdAt: 1_700_000_000_000,
  updatedAt: 1_700_000_001_000,
  ...overrides,
});

const message = (overrides: Partial<MessageRecord> = {}): MessageRecord => ({
  id: 'msg_1',
  sessionId: 'sess_a',
  role: 'assistant',
  content: '回答',
  thinking: '想一下',
  thinkingMs: 1200,
  toolCalls: [{ toolCallId: 'c1', toolName: 'search', argsText: '{}', status: 'complete' as const }],
  usage: { contextTokens: 100, contextWindow: 1000, outputTokens: 5, cacheReadTokens: 60 },
  partOrder: ['reasoning', 'text'],
  createdAt: 1_700_000_002_000,
  ...overrides,
});

const conversationRow = (overrides: Partial<ConversationRow> = {}): ConversationRow => ({
  id: 'sess_a',
  title: '服务端标题',
  model: 'auto/y',
  reasoning_effort: 'high',
  is_pinned: true,
  deleted: false,
  client_rev: 3,
  created_at: 1_700_000_000_000,
  updated_at: 1_700_000_009_000,
  version: 42,
  ...overrides,
});

const messageRow = (overrides: Partial<MessageRow> = {}): MessageRow => ({
  id: 'msg_1',
  conversation_id: 'sess_a',
  role: 'assistant',
  payload: { content: '服务端回答', thinking: '服务端的思考' },
  client_rev: 2,
  seq: 1,
  version: 43,
  created_at: 1_700_000_002_000,
  ...overrides,
});

describe('toConversationPushRow', () => {
  it('isPinned 归一成布尔、deleted 缺省为 false', () => {
    expect(toConversationPushRow(session({ isPinned: 1 }))).toMatchObject({
      is_pinned: true,
      deleted: false,
      client_rev: 0,
    });
    expect(toConversationPushRow(session({ isPinned: 0 })).is_pinned).toBe(false);
  });

  it('墓碑会原样带上去（这是删除同步的唯一信号）', () => {
    expect(toConversationPushRow(session({ deleted: true })).deleted).toBe(true);
  });
});

describe('toMessagePushRow', () => {
  it('payload 走白名单：同步元数据不会被自己推回服务端', () => {
    const row = toMessagePushRow(message({ syncState: 'pending', clientRev: 7 }));

    expect(row.payload).toMatchObject({
      content: '回答',
      thinking: '想一下',
      thinkingMs: 1200,
      partOrder: ['reasoning', 'text'],
    });
    expect(row.payload).not.toHaveProperty('syncState');
    expect(row.payload).not.toHaveProperty('clientRev');
    expect(row.payload).not.toHaveProperty('id');
    // 修订号在顶层，不在 payload 里
    expect(row.client_rev).toBe(7);
  });

  it('会话 id 映射成 conversation_id', () => {
    expect(toMessagePushRow(message({ sessionId: 'sess_z' })).conversation_id).toBe('sess_z');
  });
});

describe('fromMessageRow', () => {
  it('payload 还原成记录字段，并标成已同步', () => {
    const record = fromMessageRow(messageRow());

    expect(record).toMatchObject({
      id: 'msg_1',
      sessionId: 'sess_a',
      role: 'assistant',
      content: '服务端回答',
      thinking: '服务端的思考',
      syncState: 'synced',
      clientRev: 2,
    });
  });

  it('服务端没带 content 时补空串（流式占位/只产出思考链的历史是合法的）', () => {
    expect(fromMessageRow(messageRow({ payload: { thinking: '只有思考' } })).content).toBe('');
  });

  it('本地已有的其他字段被保留', () => {
    const local = message({ incomplete: true });
    expect(fromMessageRow(messageRow(), local).incomplete).toBe(true);
  });
});

describe('fromConversationRow', () => {
  it('布尔 is_pinned 还原成 0/1，并保留本地未知字段', () => {
    const local = session({ syncState: 'pending', clientRev: 1 });
    const record = fromConversationRow(conversationRow(), local);

    expect(record).toMatchObject({
      id: 'sess_a',
      title: '服务端标题',
      isPinned: 1,
      reasoningEffort: 'high',
      syncState: 'synced',
      clientRev: 3,
      deleted: false,
    });
  });

  it('reasoning_effort 为 null 时留空，不写成字符串 "null"', () => {
    expect(fromConversationRow(conversationRow({ reasoning_effort: null })).reasoningEffort).toBeUndefined();
  });
});

describe('shouldApplyRemote', () => {
  it('本地没有 → 建', () => {
    expect(shouldApplyRemote({ client_rev: 0 })).toBe(true);
  });

  it('本地有未推送的修改 → 不许覆盖（这是同步回滚用户内容的唯一防线）', () => {
    expect(shouldApplyRemote({ client_rev: 9 }, { syncState: 'pending', clientRev: 1 })).toBe(false);
  });

  it('本地是 error → 也不覆盖（服务端明确拒绝过，盖上去会假装成功）', () => {
    expect(shouldApplyRemote({ client_rev: 9 }, { syncState: 'error', clientRev: 1 })).toBe(false);
  });

  it('已同步：只有服务端修订号更大才代表「另一台设备又改了它」', () => {
    expect(shouldApplyRemote({ client_rev: 3 }, { syncState: 'synced', clientRev: 3 })).toBe(false);
    expect(shouldApplyRemote({ client_rev: 4 }, { syncState: 'synced', clientRev: 3 })).toBe(true);
  });

  it('老记录（没有 syncState/clientRev）按已同步处理，但不会被同修订号覆盖', () => {
    expect(shouldApplyRemote({ client_rev: 0 }, {})).toBe(false);
    expect(shouldApplyRemote({ client_rev: 1 }, {})).toBe(true);
  });
});

describe('buildPushBatch', () => {
  it('先推会话，再按会话内时间推消息', () => {
    const batch = buildPushBatch(
      [session({ id: 'sess_b' }), session({ id: 'sess_a' })],
      [
        message({ id: 'msg_b2', sessionId: 'sess_b', createdAt: 3 }),
        message({ id: 'msg_a2', sessionId: 'sess_a', createdAt: 2 }),
        message({ id: 'msg_a1', sessionId: 'sess_a', createdAt: 1 }),
      ],
      10,
    );

    expect(batch.conversations.map((row) => row.id)).toEqual(['sess_a', 'sess_b']);
    expect(batch.messages.map((row) => row.id)).toEqual(['msg_a1', 'msg_a2', 'msg_b2']);
  });

  it('会话被这一批截掉时，引用它的消息不跟着发（否则只会换回 conversation_missing）', () => {
    const batch = buildPushBatch(
      [session({ id: 'sess_a', createdAt: 1 }), session({ id: 'sess_b', createdAt: 2 })],
      [message({ id: 'msg_b1', sessionId: 'sess_b' })],
      1,
    );

    expect(batch.conversations.map((row) => row.id)).toEqual(['sess_a']);
    expect(batch.messages).toEqual([]);
  });

  it('会话已在服务端的消息照常推（它不在待推会话里，但服务端已经有那一行）', () => {
    const batch = buildPushBatch([], [message({ id: 'msg_x', sessionId: 'sess_already' })], 10);
    expect(batch.messages.map((row) => row.id)).toEqual(['msg_x']);
  });

  it('总行数不超过上限', () => {
    const sessions = Array.from({ length: 5 }, (_, index) => session({ id: `sess_${index}`, createdAt: index }));
    const messages = Array.from({ length: 20 }, (_, index) =>
      message({ id: `msg_${index}`, sessionId: 'sess_0', createdAt: index }),
    );

    const batch = buildPushBatch(sessions, messages, 8);
    expect(batch.conversations.length + batch.messages.length).toBeLessThanOrEqual(8);
  });

  it('超过一批的会话先推，消息下一批再说（不能把消息塞给还没到服务端的会话）', () => {
    const sessions = Array.from({ length: 4 }, (_, index) => session({ id: `sess_${index}`, createdAt: index }));
    const messages = sessions.map((row) => message({ id: `msg_${row.id}`, sessionId: row.id }));

    const first = buildPushBatch(sessions, messages, 2);
    expect(first.conversations).toHaveLength(2);
    expect(first.messages).toEqual([]);
  });
});
