import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { MessageRecord } from '../types'
import type { RunState } from './chat-store'
import { toRuntimeMessage } from './chat-message'
import {
  CONTEXT_MAX_MESSAGE_CHARS,
  CONTEXT_MAX_TOTAL_CHARS,
  clampContextMessages,
  loadContextMessages,
  loadSessionMessages,
  saveFinal,
  saveProgress,
  type ContextMessage,
} from './chat-persistence'

/**
 * 这些用例只跑纯逻辑，但 chat-persistence 顶层 import 了 db（会真的构造 Dexie），
 * 所以这里把 db mock 掉：`messages.where().equals().sortBy()` 返回下面这个数组。
 */
const dbMock = vi.hoisted(() => ({
  update: vi.fn(async () => 1),
  records: [] as MessageRecord[],
}))

vi.mock('../db', () => ({
  db: {
    messages: {
      update: dbMock.update,
      where: vi.fn(() => ({ equals: vi.fn(() => ({ sortBy: async () => dbMock.records })) })),
    },
    sessions: { update: vi.fn(async () => 1) },
  },
}))

const record = (
  overrides: Partial<MessageRecord> & Pick<MessageRecord, 'id' | 'role'>,
): MessageRecord => ({
  sessionId: 's1',
  content: '',
  createdAt: 1,
  ...overrides,
})

const clarification = {
  toolCallId: 'ask-1',
  question: '要 A 还是 B？',
  options: ['A 方案', 'B 方案'],
  allowCustomInput: true,
}

const makeRun = (overrides: Partial<RunState> = {}): RunState => ({
  sessionId: 's1',
  assistantMsgId: 'm1',
  model: null,
  reasoningEffort: null,
  controller: new AbortController(),
  startedAt: 0,
  thinking: '',
  content: '答题中',
  thinkingStartedAt: null,
  thinkingMs: 0,
  toolCalls: [],
  clarification: null,
  fallback: null,
  usage: null,
  error: null,
  sawDone: false,
  sawError: false,
  damaged: false,
  persistFailed: false,
  stopped: false,
  incomplete: false,
  finished: false,
  ...overrides,
})

beforeEach(() => {
  dbMock.update.mockClear()
  dbMock.records = []
})

describe('clampContextMessages', () => {
  it('未超限时内容完全不动', () => {
    const messages: ContextMessage[] = [
      { role: 'user', content: '问题' },
      { role: 'assistant', content: '回答' },
    ]

    expect(clampContextMessages(messages)).toEqual(messages)
  })

  it('不改动入参', () => {
    const messages: ContextMessage[] = [
      { role: 'assistant', content: 'x'.repeat(CONTEXT_MAX_MESSAGE_CHARS + 1) },
    ]

    clampContextMessages(messages)

    expect(messages[0].content).toHaveLength(CONTEXT_MAX_MESSAGE_CHARS + 1)
  })

  it('单条超过上限时截断（保留开头），不再让该会话每轮都 422', () => {
    const long = 'x'.repeat(CONTEXT_MAX_MESSAGE_CHARS + 12_345)

    const clamped = clampContextMessages([{ role: 'assistant', content: long }])

    expect(clamped).toHaveLength(1)
    expect(clamped[0].content).toBe(long.slice(0, CONTEXT_MAX_MESSAGE_CHARS))
  })

  it('总量超限时丢最旧，保留最近的并压回总量上限内', () => {
    // 14 条各 30000 字符 = 420000 > 380000，至少要丢 2 条
    const messages: ContextMessage[] = Array.from({ length: 14 }, (_, index) => {
      const prefix = `${index}:`
      return {
        role: 'user',
        content: prefix + 'y'.repeat(CONTEXT_MAX_MESSAGE_CHARS - prefix.length),
      }
    })

    const clamped = clampContextMessages(messages)
    const total = clamped.reduce((sum, message) => sum + message.content.length, 0)

    expect(clamped).toHaveLength(12)
    expect(clamped[0].content.startsWith('2:')).toBe(true)
    expect(clamped[clamped.length - 1].content.startsWith('13:')).toBe(true)
    expect(total).toBeLessThanOrEqual(CONTEXT_MAX_TOTAL_CHARS)
  })

  it('丢到只剩一条仍超总量时截断最长的一条', () => {
    const clamped = clampContextMessages(
      [
        { role: 'user', content: 'z'.repeat(5_000) },
        { role: 'assistant', content: 'z'.repeat(9_000) },
      ],
      { maxContentChars: 10_000, maxTotalChars: 6_000 },
    )

    expect(clamped).toHaveLength(1)
    expect(clamped[0].content).toHaveLength(6_000)
  })

  it('默认上限给后端硬上限留了安全余量（32000 / 400000）', () => {
    expect(CONTEXT_MAX_MESSAGE_CHARS).toBe(30_000)
    expect(CONTEXT_MAX_TOTAL_CHARS).toBe(380_000)
  })
})

describe('loadContextMessages', () => {
  it('把带 clarification 的空助手消息合成文本回放，用户回答不再是孤立消息', async () => {
    dbMock.records = [
      record({ id: 'u1', role: 'user', content: '帮我选一个', createdAt: 1 }),
      record({ id: 'a1', role: 'assistant', content: '', clarification, createdAt: 2 }),
      record({ id: 'u2', role: 'user', content: '选 A', createdAt: 3 }),
    ]

    const { messages: context } = await loadContextMessages('s1', { excludeId: 'placeholder', limit: 30 })

    expect(context).toHaveLength(3)
    expect(context[0]).toEqual({ role: 'user', content: '帮我选一个' })
    expect(context[1].role).toBe('assistant')
    expect(context[1].content).toContain('要 A 还是 B？')
    expect(context[1].content).toContain('A 方案')
    expect(context[1].content).toContain('B 方案')
    expect(context[1].content).toContain('用户下一条消息是对该问题的回答')
    expect(context[2]).toEqual({ role: 'user', content: '选 A' })
  })

  it('本轮占位消息之外的空白助手消息仍然丢掉', async () => {
    dbMock.records = [
      record({ id: 'u1', role: 'user', content: '你好', createdAt: 1 }),
      record({ id: 'a-empty', role: 'assistant', content: '   ', createdAt: 2 }),
      record({ id: 'a-current', role: 'assistant', content: '', createdAt: 3 }),
    ]

    const { messages: context } = await loadContextMessages('s1', { excludeId: 'a-current', limit: 30 })

    expect(context).toEqual([{ role: 'user', content: '你好' }])
  })

  it('回放结果同样受单条上限约束', async () => {
    dbMock.records = [
      record({ id: 'a1', role: 'assistant', content: 'x'.repeat(32_000), createdAt: 1 }),
    ]

    const { messages: context } = await loadContextMessages('s1', { excludeId: 'current', limit: 30 })

    expect(context[0].content).toHaveLength(CONTEXT_MAX_MESSAGE_CHARS)
  })
})

describe('落库的 incomplete 标记', () => {
  it('saveProgress 把半截内容标成 incomplete: true', async () => {
    await saveProgress('m1', makeRun())

    expect(dbMock.update).toHaveBeenCalledWith(
      'm1',
      expect.objectContaining({ content: '答题中', incomplete: true }),
    )
  })

  it('saveFinal 干净结束时写 false，覆盖 saveProgress 留下的标记', async () => {
    await saveFinal('m1', makeRun({ sawDone: true, finished: true }))

    expect(dbMock.update).toHaveBeenCalledWith(
      'm1',
      expect.objectContaining({ incomplete: false, stopped: false }),
    )
  })

  it('saveFinal 在既无 done、又无 error、也非主动停止时写 true', async () => {
    await saveFinal('m1', makeRun())

    expect(dbMock.update).toHaveBeenCalledWith('m1', expect.objectContaining({ incomplete: true }))
  })

  it('saveFinal 收到 error（合法终止）时写 false', async () => {
    await saveFinal('m1', makeRun({ sawError: true }))

    expect(dbMock.update).toHaveBeenCalledWith('m1', expect.objectContaining({ incomplete: false }))
  })

  it('saveFinal 写落停止标记', async () => {
    await saveFinal('m1', makeRun({ stopped: true }))

    expect(dbMock.update).toHaveBeenCalledWith(
      'm1',
      expect.objectContaining({ stopped: true, incomplete: false }),
    )
  })
})

describe('partOrder 落库与读回', () => {
  it('saveProgress 把事件到达顺序一并写入', async () => {
    await saveProgress('m1', makeRun({ partOrder: ['tool-call', 'reasoning', 'text'] }))

    // 缺了它，刷新后思考/工具/正文只能按默认顺序近似还原
    expect(dbMock.update).toHaveBeenCalledWith(
      'm1',
      expect.objectContaining({ partOrder: ['tool-call', 'reasoning', 'text'] }),
    )
  })

  it('saveFinal 同样写入（终态不能把顺序丢掉）', async () => {
    await saveFinal('m1', makeRun({ partOrder: ['reasoning', 'text'] }))

    expect(dbMock.update).toHaveBeenCalledWith(
      'm1',
      expect.objectContaining({ partOrder: ['reasoning', 'text'] }),
    )
  })

  it('run 上没有顺序表时写空数组', async () => {
    await saveProgress('m1', makeRun())

    // 空数组读回等价于「没有顺序」（readPartOrder 退回默认顺序），不会崩
    expect(dbMock.update).toHaveBeenCalledWith('m1', expect.objectContaining({ partOrder: [] }))
  })

  it('loadSessionMessages 读出的顺序能把工具夹在正文两侧还原', async () => {
    dbMock.records = [
      record({
        id: 'a1',
        role: 'assistant',
        content: '中间的话',
        toolCalls: [
          { toolCallId: 'c1', toolName: 't', argsText: '{}', status: 'complete' },
          { toolCallId: 'c2', toolName: 't', argsText: '{}', status: 'complete' },
        ],
        partOrder: ['tool-call', 'text', 'tool-call'],
        createdAt: 1,
      }),
    ]

    const [loaded] = await loadSessionMessages('s1')
    const parts = toRuntimeMessage(loaded).content as unknown as Array<{ type: string }>

    expect(parts.map((part) => part.type)).toEqual(['tool-call', 'text', 'tool-call'])
  })
})

describe('本轮模型的落库（上下文锁按模型区分）', () => {
  it('saveFinal 把本轮请求的模型写进记录', async () => {
    await saveFinal('m1', makeRun({ model: 'group/auto-tiny', sawDone: true, finished: true }))

    expect(dbMock.update).toHaveBeenCalledWith('m1', expect.objectContaining({ model: 'group/auto-tiny' }))
  })

  it('run 上没有模型时写 null，而不是把字段漏掉', async () => {
    // 漏掉字段会让「上下文锁在哪个模型上」读回 undefined，语义从「未知」漂成「没有」
    await saveProgress('m1', makeRun({ model: null }))

    expect(dbMock.update).toHaveBeenCalledWith('m1', expect.objectContaining({ model: null }))
  })
})

describe('真实用量的落库', () => {
  const usage = { contextTokens: 1066, contextWindow: 1048000, outputTokens: 2, cacheReadTokens: 896 }

  it('saveFinal 把用量写进记录（刷新后「上下文用了多少」仍然可见）', async () => {
    await saveFinal('m1', makeRun({ usage, sawDone: true, finished: true }))

    expect(dbMock.update).toHaveBeenCalledWith('m1', expect.objectContaining({ usage }))
  })

  it('没有用量时写 null，而不是把字段漏掉', async () => {
    // 漏掉字段会让读回的是 undefined，与「上游没报」这个语义区分不开
    await saveProgress('m1', makeRun({ usage: null }))

    expect(dbMock.update).toHaveBeenCalledWith('m1', expect.objectContaining({ usage: null }))
  })
})

describe('真实用量锚点（给服务端的上下文预算用）', () => {
  const usage = { contextTokens: 1065, contextWindow: 1048000, outputTokens: 2, cacheReadTokens: 896 }

  /** 14 条贴满单条上限的记录（合计 420k 字符，必然触发总量裁剪）；usageOn 指定谁带用量。 */
  const bigHistory = (usageOn: Record<string, number>): MessageRecord[] =>
    Array.from({ length: 14 }, (_, index) => {
      const id = `m${index}`
      const tokens = usageOn[id]
      return record({
        id,
        role: index % 2 === 0 ? 'user' : 'assistant',
        content: 'x'.repeat(32_000),
        createdAt: index + 1,
        usage: tokens ? { ...usage, contextTokens: tokens } : null,
      })
    })

  it('取最后一条带用量的助手消息，下标对应真正发出去的数组', async () => {
    dbMock.records = [
      record({ id: 'u1', role: 'user', content: '问', createdAt: 1 }),
      record({ id: 'a1', role: 'assistant', content: '答', usage, createdAt: 2 }),
      record({ id: 'u2', role: 'user', content: '再问', createdAt: 3 }),
    ]

    const { messages, anchor } = await loadContextMessages('s1', { excludeId: 'current', limit: 30 })

    expect(anchor).toEqual({ messageIndex: 1, contextTokens: 1065 })
    // 锚点字段不能跟着发上游：协议里只该有 role / content
    expect(messages[1]).toEqual({ role: 'assistant', content: '答' })
  })

  it('空白的助手占位消息被滤掉后，下标跟着前移', async () => {
    // 这是最容易错的一处：锚点下标必须按「滤过、裁过之后」的数组算
    dbMock.records = [
      record({ id: 'u1', role: 'user', content: '问', createdAt: 1 }),
      record({ id: 'a1', role: 'assistant', content: '答', usage, createdAt: 2 }),
      record({ id: 'a-empty', role: 'assistant', content: '   ', createdAt: 3 }),
      record({ id: 'u2', role: 'user', content: '再问', createdAt: 4 }),
    ]

    const { messages, anchor } = await loadContextMessages('s1', { excludeId: 'current', limit: 30 })

    expect(messages).toHaveLength(3)
    expect(anchor).toEqual({ messageIndex: 1, contextTokens: 1065 })
  })

  it('没有任何用量时没有锚点（首轮 / 老记录 / 上游没报）', async () => {
    dbMock.records = [
      record({ id: 'u1', role: 'user', content: '问', createdAt: 1 }),
      record({ id: 'a1', role: 'assistant', content: '答', createdAt: 2 }),
      record({ id: 'u2', role: 'user', content: '再问', createdAt: 3 }),
    ]

    const { anchor } = await loadContextMessages('s1', { excludeId: 'current', limit: 30 })

    expect(anchor).toBeNull()
  })

  it('用量为 0 不当锚点（那只会把预算算小，等于放行一次必然超限的请求）', async () => {
    dbMock.records = [
      record({ id: 'u1', role: 'user', content: '问', createdAt: 1 }),
      record({
        id: 'a1',
        role: 'assistant',
        content: '答',
        usage: { ...usage, contextTokens: 0 },
        createdAt: 2,
      }),
      record({ id: 'u2', role: 'user', content: '再问', createdAt: 3 }),
    ]

    const { anchor } = await loadContextMessages('s1', { excludeId: 'current', limit: 30 })

    expect(anchor).toBeNull()
  })

  it('锚点所在的消息被裁掉时没有锚点（不能拿一个已不在数组里的位置去算）', async () => {
    // 14 条 × 30k 字符（单条先被截到 30k）= 420k > 380k 总量上限 → 最旧的两条被丢。
    // 用量只挂在 m1 上，它正好被丢掉：此时必须放弃锚点、退回估算，而不是继续用一个
    // 指向别处的下标。
    dbMock.records = bigHistory({ m1: 111 })

    const { messages, anchor } = await loadContextMessages('s1', { excludeId: 'current', limit: 30 })

    expect(messages).toHaveLength(12)
    expect(anchor).toBeNull()
  })

  it('裁剪后锚点下标跟着前移，仍指向带用量那条消息', async () => {
    dbMock.records = bigHistory({ m7: 222 })

    const { messages, anchor } = await loadContextMessages('s1', { excludeId: 'current', limit: 30 })

    expect(messages).toHaveLength(12)
    // m7 原在第 7 位，丢掉最旧两条后落在第 5 位
    expect(anchor).toEqual({ messageIndex: 5, contextTokens: 222 })
    expect(messages[5].role).toBe('assistant')
  })
})
