import { describe, expect, it } from 'vitest';

import type { MessageRecord, SessionRecord } from '../../types';
import {
  buildPushBatch,
  fromConversationRow,
  fromMessageRow,
  sanitizeMessagePayload,
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
  usage: {
    contextTokens: 100,
    contextWindow: 1000,
    contextLimitTokens: 700,
    outputTokens: 5,
    cacheReadTokens: 60,
  },
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

/** 一条待审批调用。跨设备那组用例要一个完整形状，抽出来避免每处各写一遍。 */
const dangerousApprovalCall = {
  toolCallId: 'call_1',
  toolName: 'delete_document',
  argsText: '{"document_id": 42}',
  description: '删除一篇知识库文档（不可撤销）',
  riskLevel: 'dangerous',
};

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

  /**
   * M4：审批轮必须能出本机。
   *
   * 这是「白名单」最容易复发的一类 bug——新增业务字段时忘了加进名单，字段在本机
   * 一切正常（落库、渲染都对），只在**换一台设备**时才消失。所以断言直接盯住 payload：
   * 一旦有人把 `approval` 从名单里删掉，用例立刻失败。
   */
  it('待审批的调用进 payload：跨设备才能看到审批卡片并继续这一轮', () => {
    const row = toMessagePushRow(
      message({
        approval: {
          toolCalls: [
            {
              toolCallId: 'call_1',
              toolName: 'delete_document',
              argsText: '{"document_id": 42}',
              description: '删除一篇知识库文档（不可撤销）',
              riskLevel: 'dangerous',
            },
          ],
        },
      }),
    );

    expect(row.payload.approval).toEqual({
      toolCalls: [
        {
          toolCallId: 'call_1',
          toolName: 'delete_document',
          argsText: '{"document_id": 42}',
          description: '删除一篇知识库文档（不可撤销）',
          riskLevel: 'dangerous',
        },
      ],
    });
  });

  it('没有待审批时 payload 里不出现 approval 键（老记录不会凭空多出一个 null）', () => {
    expect(toMessagePushRow(message())).not.toHaveProperty('payload.approval');
    expect(toMessagePushRow(message({ approval: null })).payload).toHaveProperty('approval', null);
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


describe('sanitizeMessagePayload（L-08）', () => {
  it('坏掉的 thinking / 非数组 partOrder 被丢弃，不让 React 渲染对象', () => {
    const payload = sanitizeMessagePayload({
      content: 'ok',
      thinking: { a: 1 },
      thinkingMs: 'soon',
      partOrder: ['text', 'bogus', 42],
    });

    expect(payload.content).toBe('ok');
    expect(payload.thinking).toBeUndefined();
    expect(payload.thinkingMs).toBeUndefined();
    expect(payload.partOrder).toEqual(['text']);
  });

  it('结构不完整的工具调用 / 澄清 / 用量 / 错误整条丢弃', () => {
    const payload = sanitizeMessagePayload({
      toolCalls: [
        { toolCallId: 'c1', toolName: 'search', argsText: '{}', status: 'complete', outcome: 'success' },
        { toolCallId: 'c2', toolName: 'search' },
        { toolCallId: 'c3', toolName: 'x', argsText: '{}', status: 'weird' },
      ],
      clarification: { toolCallId: 'c1', question: { evil: true }, options: ['a', 1], allowCustomInput: true },
      usage: { contextTokens: '100' },
      error: { message: 42 },
    });

    expect(payload.toolCalls).toEqual([
      { toolCallId: 'c1', toolName: 'search', argsText: '{}', status: 'complete', outcome: 'success' },
    ]);
    expect(payload.clarification).toBeUndefined();
    expect(payload.usage).toBeUndefined();
    expect(payload.error).toBeUndefined();
  });

  it('合法的嵌套结构原样保留', () => {
    const payload = sanitizeMessagePayload({
      content: '回答',
      toolCalls: [{ toolCallId: 'c1', toolName: 'search', argsText: '{}', status: 'running' }],
      clarification: { toolCallId: 'c1', question: '选哪个？', options: ['A', 'B'], allowCustomInput: false },
      fallback: { fromModel: 'a', toModel: 'b', reason: '上游 503' },
      compaction: {
        strategy: 'summarize',
        droppedMessages: 4,
        truncatedMessages: 1,
        usedTokensBefore: 90_000,
        usedTokensAfter: 20_000,
        limitTokens: 30_000,
      },
      usage: { contextTokens: 10, contextWindow: 100, contextLimitTokens: 80, outputTokens: 2, cacheReadTokens: 0 },
      error: { message: '失败', retryable: true, code: 'internal_error' },
    });

    expect(payload.content).toBe('回答');
    expect(payload.toolCalls?.[0].status).toBe('running');
    expect(payload.clarification?.question).toBe('选哪个？');
    expect(payload.fallback?.reason).toBe('上游 503');
    // 压缩事实同样跨设备可见：另一台设备刷新后也该知道「这一轮之前压缩过」
    expect((payload.compaction as { strategy: string }).strategy).toBe('summarize');
    expect(payload.usage?.contextWindow).toBe(100);
    expect(payload.error?.retryable).toBe(true);
  });

  it('payload 不是对象时返回空对象，而不是抛错', () => {
    expect(sanitizeMessagePayload(null)).toEqual({});
    expect(sanitizeMessagePayload('[]')).toEqual({});
  });
});

describe('sanitizeMessagePayload：compaction', () => {
  it('合法记录原样保留', () => {
    const payload = sanitizeMessagePayload({
      compaction: {
        strategy: 'summarize',
        droppedMessages: 4,
        truncatedMessages: 1,
        usedTokensBefore: 90_000,
        usedTokensAfter: 20_000,
        limitTokens: 30_000,
      },
    });

    expect(payload.compaction).toEqual({
      strategy: 'summarize',
      droppedMessages: 4,
      truncatedMessages: 1,
      usedTokensBefore: 90_000,
      usedTokensAfter: 20_000,
      limitTokens: 30_000,
    });
  });

  it('协议外的 strategy 整条丢掉（它是渲染分支的判据）', () => {
    // 带一个未知字符串进去，显示层会落到空白分支：用户看到一行没有文字的图标
    expect(
      sanitizeMessagePayload({
        compaction: {
          strategy: 'rewrite',
          usedTokensBefore: 1,
          usedTokensAfter: 1,
          limitTokens: 1,
        },
      }).compaction,
    ).toBeUndefined();
  });

  it('缺必要数字时整条丢掉，不显示「压缩了 0 条」', () => {
    expect(
      sanitizeMessagePayload({ compaction: { strategy: 'summarize' } }).compaction,
    ).toBeUndefined();
  });

  it('可选计数缺失时补零', () => {
    const payload = sanitizeMessagePayload({
      compaction: { strategy: 'truncate', usedTokensBefore: 10, usedTokensAfter: 5, limitTokens: 100 },
    });

    expect(payload.compaction).toEqual({
      strategy: 'truncate',
      droppedMessages: 0,
      truncatedMessages: 0,
      usedTokensBefore: 10,
      usedTokensAfter: 5,
      limitTokens: 100,
    });
  });

  it('非对象一律当缺席（老服务端不带这个字段）', () => {
    expect(sanitizeMessagePayload({ compaction: 'oops' }).compaction).toBeUndefined();
    expect(sanitizeMessagePayload({}).compaction).toBeUndefined();
  });
});

describe('sanitizeMessagePayload：approval（M4 跨设备）', () => {
  const call = (overrides: Record<string, unknown> = {}) => ({
    toolCallId: 'call_1',
    toolName: 'delete_document',
    argsText: '{"document_id": 42}',
    description: '删除一篇知识库文档（不可撤销）',
    riskLevel: 'dangerous',
    ...overrides,
  });

  it('合法记录原样还原：另一台设备据此渲染审批卡片并续跑', () => {
    const payload = sanitizeMessagePayload({ approval: { toolCalls: [call()] } });

    expect(payload.approval).toEqual({
      toolCalls: [
        {
          toolCallId: 'call_1',
          toolName: 'delete_document',
          argsText: '{"document_id": 42}',
          description: '删除一篇知识库文档（不可撤销）',
          riskLevel: 'dangerous',
        },
      ],
    });
  });

  it('push → pull 往返一致：白名单与守卫两头都对上才有效', () => {
    const original = {
      approval: { toolCalls: [call({ toolCallId: 'call_9', riskLevel: 'safe_write' })] },
    };
    const pushed = toMessagePushRow(message(original)).payload;
    // 经过一次 JSON 往返（真实同步就是这样过了一遍网络）
    const pulled = sanitizeMessagePayload(JSON.parse(JSON.stringify(pushed)));

    expect(pulled.approval).toEqual(original.approval);
  });

  it('只要有一条调用坏掉就整块丢掉：半个审批框比没有更危险', () => {
    // 少一条调用 → 用户在一个不完整的清单上「提交决定」，
    // 提交出去的决定集合与那一轮真实被拦下的调用对不上
    expect(
      sanitizeMessagePayload({
        approval: { toolCalls: [call(), { toolCallId: 'call_2', toolName: 'x' }] },
      }).approval,
    ).toBeUndefined();
    expect(sanitizeMessagePayload({ approval: { toolCalls: [call(), 'nope'] } }).approval).toBeUndefined();
    expect(sanitizeMessagePayload({ approval: { toolCalls: [call(), null] } }).approval).toBeUndefined();
  });

  it('缺 toolCallId / toolName / argsText 的那条会让整块失效（三个都是判断与表态的必需项）', () => {
    for (const key of ['toolCallId', 'toolName', 'argsText']) {
      expect(
        sanitizeMessagePayload({ approval: { toolCalls: [call({ [key]: undefined })] } }).approval,
        `缺 ${key} 时应整块丢掉`,
      ).toBeUndefined();
      expect(
        sanitizeMessagePayload({ approval: { toolCalls: [call({ [key]: 42 })] } }).approval,
        `${key} 类型不对时应整块丢掉`,
      ).toBeUndefined();
    }
  });

  it('空清单整块丢掉：0 条待确认的卡片既不能提交也不能忽略', () => {
    expect(sanitizeMessagePayload({ approval: { toolCalls: [] } }).approval).toBeUndefined();
    expect(sanitizeMessagePayload({ approval: {} }).approval).toBeUndefined();
    expect(sanitizeMessagePayload({ approval: { toolCalls: 'nope' } }).approval).toBeUndefined();
    expect(sanitizeMessagePayload({ approval: 'oops' }).approval).toBeUndefined();
  });

  it('坏数据（非 null）一律当缺席，不动本地字段', () => {
    // 与下面那条对照：只有**显式 null** 才是清除信号，其余坏形状都不能冒充它，
    // 否则一条脏数据就能把本机还挂着的审批卡片抹掉。
    for (const broken of [{}, { toolCalls: [] }, { toolCalls: 'nope' }, 'oops', 42]) {
      expect(sanitizeMessagePayload({ approval: broken })).not.toHaveProperty('approval');
    }
  });

  it('description / riskLevel 缺失时退化成空串，不因此丢掉整块', () => {
    // 少了风险等级会走「未知风险、按最高风险呈现」，是安全的一侧；
    // 少了说明只是卡片少一行，决定仍然可以做出。
    const payload = sanitizeMessagePayload({
      approval: {
        toolCalls: [{ toolCallId: 'c1', toolName: 'shell', argsText: '{"cmd":"ls"}' }],
      },
    });

    expect(payload.approval).toEqual({
      toolCalls: [{ toolCallId: 'c1', toolName: 'shell', argsText: '{"cmd":"ls"}', description: '', riskLevel: '' }],
    });
  });

  it('与 toolCalls 的容错策略不同：toolCalls 丢坏条，approval 丢整块', () => {
    const payload = sanitizeMessagePayload({
      toolCalls: [
        { toolCallId: 'c1', toolName: 'search', argsText: '{}', status: 'complete' },
        { toolCallId: 'c2', toolName: 'search' },
      ],
      approval: { toolCalls: [call(), { toolCallId: 'c3', toolName: 'x' }] },
    });

    // 执行历史可以只留能看懂的；审批清单不行（漏一条 = 用户以为看到了全部）
    expect(payload.toolCalls).toHaveLength(1);
    expect(payload.approval).toBeUndefined();
  });
});

/**
 * M3 × M4：「忽略审批」必须能跨设备生效。
 *
 * 这两块单独看都对，合起来有个洞：M4 把 `approval` 加进白名单后，审批轮会带着
 * `approval.toolCalls` 推到服务端；设备 B 忽略它时如果**只**把本地的 `approval` 清掉、
 * 不推一个 `approval: null` 上行，那么设备 A 下次 pull 拿到的仍是带 `toolCalls` 的版本
 * ——卡片又冒出来，而且再点「忽略」也一样（服务端那份从来没被清过）。
 *
 * 所以 `approval: null` 必须是**可同步的清除信号**，而不是一个被守卫丢掉的空值。
 * 这一组用例把两端都钉住：推出去的 payload 里要真有 `approval: null`，拉回来时又要
 * 能让本地卡片消失。
 */
describe('approval: null 是跨设备的清除信号', () => {
  it('toMessagePushRow 把 approval: null 原样带进 payload（不能被当成「没有字段」跳过）', () => {
    const row = toMessagePushRow(message({ approval: null }));

    // 白名单循环用的是 `record[key] !== undefined`：null 会通过，这正是我们要的。
    // 如果哪天有人把它改成 truthy 判断，清除信号就会被静默丢掉，这条会失败。
    expect(row.payload).toHaveProperty('approval', null);
  });

  it('approval 缺席（老记录）时 payload 里就没有这个键', () => {
    expect(toMessagePushRow(message())).not.toHaveProperty('payload.approval');
  });

  it('sanitizeMessagePayload 遇 approval: null 不报错，也不造一张空卡片出来', () => {
    const payload = sanitizeMessagePayload({ content: 'x', approval: null });

    expect(payload.content).toBe('x');
    // 关键：不能返回 { toolCalls: [] } 这种半成品——那会渲染出一张
    // 「0 条待确认」、既不能提交也不能忽略的卡片。
    // 同时它必须是显式 `null`（清除信号），不能是 undefined（=「服务端没说」）。
    expect(payload.approval).toBeNull();
  });

  it('拉到一个 approval: null 时，本地的旧审批必须被清掉', () => {
    // 模拟设备 A：本地已经有一条带审批的消息
    const local = message({
      approval: { toolCalls: [dangerousApprovalCall] },
      syncState: 'synced',
      clientRev: 3,
    });
    // 设备 B 忽略后推上去的那一行（clientRev 前进了，approval 是 null）
    const remote = messageRow({
      client_rev: 4,
      payload: { content: '回答', approval: null },
    });

    const merged = fromMessageRow(remote, local);

    // 卡片要消失。若 sanitizeApproval 把 null 丢掉，`{...local, ...payload}` 会让
    // local.approval 活下来 —— 设备 A 的卡片就永远删不掉。
    expect(merged.approval).toBeNull();
  });

  it('反过来：拉到带 toolCalls 的行时，本地被清掉的审批会重新出现', () => {
    const local = message({ approval: null, syncState: 'synced', clientRev: 2 });
    const remote = messageRow({
      client_rev: 5,
      payload: { content: '回答', approval: { toolCalls: [dangerousApprovalCall] } },
    });

    expect(fromMessageRow(remote, local).approval).toEqual({
      toolCalls: [dangerousApprovalCall],
    });
  });
});
