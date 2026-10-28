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

/** 推送引擎：`saveFinal`/`clearMessageApproval` 尾巴上都要叫醒它。 */
const pushSpy = vi.hoisted(() => vi.fn())
vi.mock('./sync/engine', () => ({ requestPush: pushSpy }))

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
  approval: null,
  fallback: null,
  compaction: null,
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

  it('本轮占位消息（excludeId）不进历史，空白助手消息改为合成占位文本', async () => {
    dbMock.records = [
      record({ id: 'u1', role: 'user', content: '你好', createdAt: 1 }),
      record({ id: 'a-empty', role: 'assistant', content: '   ', createdAt: 2 }),
      record({ id: 'u2', role: 'user', content: '再问', createdAt: 3 }),
      record({ id: 'a-current', role: 'assistant', content: '', createdAt: 4 }),
    ]

    const { messages: context } = await loadContextMessages('s1', { excludeId: 'a-current', limit: 30 })

    // 本轮自己的空占位不进历史；a-empty 是上一轮的（用户按 Esc 停掉的那一轮），
    // 必须留下并合成占位文本——丢掉它会让历史变成两条相邻的 user，后端 422。
    expect(context).toEqual([
      { role: 'user', content: '你好' },
      { role: 'assistant', content: '（本轮未产生回答）' },
      { role: 'user', content: '再问' },
    ])
  })

  it('回放结果同样受单条上限约束', async () => {
    dbMock.records = [
      record({ id: 'u1', role: 'user', content: '问', createdAt: 1 }),
      record({ id: 'a1', role: 'assistant', content: 'x'.repeat(32_000), createdAt: 2 }),
      record({ id: 'u2', role: 'user', content: '再问', createdAt: 3 }),
    ]

    const { messages: context } = await loadContextMessages('s1', { excludeId: 'current', limit: 30 })

    // 中间那条被截到上限；首尾两条不受影响
    expect(context).toHaveLength(3)
    expect(context[1].content).toHaveLength(CONTEXT_MAX_MESSAGE_CHARS)
    expect(context[0]).toEqual({ role: 'user', content: '问' })
    expect(context[2]).toEqual({ role: 'user', content: '再问' })
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

  it('loadSessionMessages 保留到达顺序：工具按顺序排，正文整块排在最后', async () => {
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

    // 顺序表里正文夹在两个调用之间，但正文只有一个块、无法拆开去夹（见 buildParts 注释）
    expect(parts.map((part) => part.type)).toEqual(['tool-call', 'tool-call', 'text'])
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
  const usage = {
    contextTokens: 1066,
    contextWindow: 1048000,
    contextLimitTokens: 648000,
    outputTokens: 2,
    cacheReadTokens: 896,
  }

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
  const usage = {
    contextTokens: 1065,
    contextWindow: 1048000,
    contextLimitTokens: 648000,
    outputTokens: 2,
    cacheReadTokens: 896,
  }

  /**
   * 14 条贴满单条上限的记录（合计 420k 字符，必然触发总量裁剪）；usageOn 指定谁带用量。
   *
   * 角色从 user 起严格交替，所以末条是 assistant —— 裁剪前要先按「末条必须是 user」
   * 把它去掉，于是进入字符裁剪的是 13 条（首条 user 起、末条 user 收）。
   */
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

  it('相邻的空白助手消息被合成占位文本后，下标跟着前移', async () => {
    // 空正文的助手消息不再被丢掉（丢掉会造出相邻 user/user，后端 422），而是合成占位
    // 文本；它和前一条助手消息同角色，于是被合并成一条。锚点下标必须按「合并、裁过
    // 之后」的数组算——这是最容易错的一处。
    dbMock.records = [
      record({ id: 'u1', role: 'user', content: '问', createdAt: 1 }),
      record({ id: 'a1', role: 'assistant', content: '答', usage, createdAt: 2 }),
      record({ id: 'a-empty', role: 'assistant', content: '   ', createdAt: 3 }),
      record({ id: 'u2', role: 'user', content: '再问', createdAt: 4 }),
    ]

    const { messages, anchor } = await loadContextMessages('s1', { excludeId: 'current', limit: 30 })

    expect(messages).toHaveLength(3)
    expect(messages[1].content).toContain('答')
    expect(messages[1].content).toContain('本轮未产生回答')
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
    // 末条 assistant 先被去掉（13 条），单条截到 30k = 390k > 380k 总量上限 → 再丢最旧的
    // 一条（m0）。丢完首条变成 m1（assistant），形状对齐时又往后推一条，于是 m1 也离开
    // 了数组。用量正好挂在 m1 上：此时必须放弃锚点、退回估算，而不是继续用一个指向
    // 别处的下标。
    dbMock.records = bigHistory({ m1: 111 })

    const { messages, anchor } = await loadContextMessages('s1', { excludeId: 'current', limit: 30 })

    expect(messages).toHaveLength(11)
    expect(anchor).toBeNull()
  })

  it('裁剪后锚点下标跟着前移，仍指向带用量那条消息', async () => {
    dbMock.records = bigHistory({ m7: 222 })

    const { messages, anchor } = await loadContextMessages('s1', { excludeId: 'current', limit: 30 })

    expect(messages).toHaveLength(11)
    // m7 原在第 7 位，丢掉最旧两条后落在第 5 位
    expect(anchor).toEqual({ messageIndex: 5, contextTokens: 222 })
    expect(messages[5].role).toBe('assistant')
  })
})

/**
 * 「会话永久 422 死锁」的回归锁。
 *
 * 这一组用例都是**在修之前必失败**的：它们直接断言「送出去的数组形状合法」，
 * 而形状非法的表现是真机上的一次 422——那是用户唯一能看到的症状，且永久无法自愈
 * （历史只增不减，下一轮同样非法）。
 */
describe('回放历史的形状（后端严格要求首条 user / 严格交替 / 末条 user）', () => {
  /** 造一段 n 轮真实对话：每轮一问一答，末尾再补一条新的用户提问。长度恒为 2n+1。 */
  const conversation = (turns: number): MessageRecord[] => {
    const records: MessageRecord[] = []
    for (let index = 0; index < turns; index += 1) {
      records.push(
        record({ id: `u${index}`, role: 'user', content: `问 ${index}`, createdAt: index * 2 }),
        record({ id: `a${index}`, role: 'assistant', content: `答 ${index}`, createdAt: index * 2 + 1 }),
      )
    }
    records.push(record({ id: 'u-latest', role: 'user', content: '最新的提问', createdAt: 9999 }))
    return records
  }

  /** 后端 `_check_message_order` 的三条规则，逐条复刻。 */
  const expectBackendShape = (messages: readonly ContextMessage[]): void => {
    expect(messages.length).toBeGreaterThan(0)
    expect(messages[0].role).toBe('user')
    for (let index = 1; index < messages.length; index += 1) {
      expect(messages[index].role).not.toBe(messages[index - 1].role)
    }
    expect(messages[messages.length - 1].role).toBe('user')
  }

  it('15 轮（31 条）切到 30 条时首条仍是 user —— slice(-30) 是老死锁的直接原因', async () => {
    // 31 条是奇数、HISTORY_LIMIT 是偶数 30，切点必然落在 assistant 上：
    // 老代码直接把 [assistant, user, …, user] 发出去，后端第一句就 422。
    dbMock.records = conversation(15)

    const { messages } = await loadContextMessages('s1', { excludeId: 'placeholder', limit: 30 })

    expect(messages).toHaveLength(29)
    expectBackendShape(messages)
    // 保住的是最近的 29 条：最早那条「问 0」被丢掉，首条应从「答 0」之后的「问 1」开始
    expect(messages[0]).toEqual({ role: 'user', content: '问 1' })
    expect(messages[messages.length - 1]).toEqual({ role: 'user', content: '最新的提问' })
  })

  it('14 / 15 / 16 / 20 轮都给出合法形状（老代码只有 14 轮侥幸成立）', async () => {
    for (const turns of [14, 15, 16, 20]) {
      dbMock.records = conversation(turns)
      const { messages } = await loadContextMessages('s1', { excludeId: 'placeholder', limit: 30 })
      expectBackendShape(messages)
    }
  })

  it('发送后首 token 之前按 Esc 停止（落库空助手）不会造出相邻 user', async () => {
    // 老代码把空助手消息整条滤掉 → [user, user] → 422「第 1 条与第 2 条都是 user」
    dbMock.records = [
      record({ id: 'u1', role: 'user', content: '第一个问题', createdAt: 1 }),
      record({ id: 'a1', role: 'assistant', content: '', stopped: true, createdAt: 2 }),
      record({ id: 'u2', role: 'user', content: '第二个问题', createdAt: 3 }),
    ]

    const { messages } = await loadContextMessages('s1', { excludeId: 'placeholder', limit: 30 })

    expectBackendShape(messages)
    expect(messages[1]).toEqual({ role: 'assistant', content: '（本轮已停止，未产生回答）' })
  })

  it('审批轮点了「忽略」（正文仍空）不会造出相邻 user', async () => {
    // dismissApproval 只清 approval，正文本来就是空的：老代码于是同样切出 [user, user]
    dbMock.records = [
      record({ id: 'u1', role: 'user', content: '删掉这些文件', createdAt: 1 }),
      record({ id: 'a1', role: 'assistant', content: '', stopped: false, createdAt: 2 }),
      record({ id: 'u2', role: 'user', content: '算了我自己来', createdAt: 3 }),
    ]

    const { messages } = await loadContextMessages('s1', { excludeId: 'placeholder', limit: 30 })

    expectBackendShape(messages)
    expect(messages[1].content).toBe('（本轮未产生回答）')
  })

  it('未忽略的待审批轮用「待审批，未执行」占位，不让模型以为用户放弃了', async () => {
    dbMock.records = [
      record({ id: 'u1', role: 'user', content: '删掉这些文件', createdAt: 1 }),
      record({
        id: 'a1',
        role: 'assistant',
        content: '',
        approval: {
          toolCalls: [
            { toolCallId: 't1', toolName: 'rm', argsText: '{}', description: '', riskLevel: 'dangerous' },
          ],
        },
        createdAt: 2,
      }),
      record({ id: 'u2', role: 'user', content: '先等一下', createdAt: 3 }),
    ]

    const { messages } = await loadContextMessages('s1', { excludeId: 'placeholder', limit: 30 })

    expectBackendShape(messages)
    expect(messages[1].content).toBe('（本轮工具调用待审批，未执行）')
  })

  it('失败轮的空正文用「请求失败」占位', async () => {
    dbMock.records = [
      record({ id: 'u1', role: 'user', content: '在吗', createdAt: 1 }),
      record({
        id: 'a1',
        role: 'assistant',
        content: '',
        error: { message: '请求失败', retryable: true, code: null },
        createdAt: 2,
      }),
      record({ id: 'u2', role: 'user', content: '还在吗', createdAt: 3 }),
    ]

    const { messages } = await loadContextMessages('s1', { excludeId: 'placeholder', limit: 30 })

    expectBackendShape(messages)
    expect(messages[1].content).toBe('（本轮请求失败，未产生回答）')
  })

  it('空白 user 消息不进历史（后端 _require_user_content 必拒）', async () => {
    dbMock.records = [
      record({ id: 'u1', role: 'user', content: '   ', createdAt: 1 }),
      record({ id: 'a1', role: 'assistant', content: '答', createdAt: 2 }),
      record({ id: 'u2', role: 'user', content: '真的问题', createdAt: 3 }),
    ]

    const { messages } = await loadContextMessages('s1', { excludeId: 'placeholder', limit: 30 })

    expectBackendShape(messages)
    expect(messages).toEqual([{ role: 'user', content: '真的问题' }])
  })

  it('末条是 assistant（上一轮还没等到新提问）时被去掉', async () => {
    dbMock.records = [
      record({ id: 'u1', role: 'user', content: '问', createdAt: 1 }),
      record({ id: 'a1', role: 'assistant', content: '答', createdAt: 2 }),
    ]

    const { messages } = await loadContextMessages('s1', { excludeId: 'placeholder', limit: 30 })

    expectBackendShape(messages)
    expect(messages).toEqual([{ role: 'user', content: '问' }])
  })

  it('同角色相邻的消息合成一条，不静默丢掉任何一轮的内容', async () => {
    dbMock.records = [
      record({ id: 'u1', role: 'user', content: '第一句', createdAt: 1 }),
      record({ id: 'u2', role: 'user', content: '补充一句', createdAt: 2 }),
      record({ id: 'a1', role: 'assistant', content: '答', createdAt: 3 }),
      record({ id: 'u3', role: 'user', content: '新问题', createdAt: 4 }),
    ]

    const { messages } = await loadContextMessages('s1', { excludeId: 'placeholder', limit: 30 })

    expectBackendShape(messages)
    // 两条都保留在同一格里：丢掉任何一条都会让用户说过的话凭空消失
    expect(messages[0].content).toBe('第一句\n\n补充一句')
  })

  it('字符裁剪吃掉开头后重新对齐形状（clampContextMessages 也会切出 assistant 开头）', async () => {
    // 29 条各 30_000 字符 = 870k，远超 380k 总量上限 → 丢最旧直到 12 条（360k）。
    // 29 - 12 = 17 条被丢掉，第 17 条（下标 17）是 assistant：裁剪后的首条就是 assistant，
    // 老代码到这里同样发不出去。
    const records: MessageRecord[] = []
    for (let index = 0; index < 14; index += 1) {
      records.push(
        record({ id: `u${index}`, role: 'user', content: 'x'.repeat(30_000), createdAt: index * 2 }),
        record({ id: `a${index}`, role: 'assistant', content: 'x'.repeat(30_000), createdAt: index * 2 + 1 }),
      )
    }
    records.push(record({ id: 'u-latest', role: 'user', content: 'x'.repeat(30_000), createdAt: 9999 }))
    dbMock.records = records

    const { messages, anchor } = await loadContextMessages('s1', { excludeId: 'placeholder', limit: 30 })

    expectBackendShape(messages)
    // 裁剪后再对齐会再让掉一条 assistant，于是剩 11 条
    expect(messages).toHaveLength(11)
    expect(anchor).toBeNull()
  })
})

describe('回放历史不携带系统提示', () => {
  it('失败轮正文尾部的「请求失败」引用块不发给模型', async () => {
    dbMock.records = [
      record({ id: 'u1', role: 'user', content: '在吗', createdAt: 1 }),
      record({
        id: 'a1',
        role: 'assistant',
        content: '在的。\n\n> ⚠️ **请求失败**: 网络已断开',
        error: { message: '网络已断开', retryable: true, code: null },
        createdAt: 2,
      }),
      record({ id: 'u2', role: 'user', content: '那就继续', createdAt: 3 }),
    ]

    const { messages } = await loadContextMessages('s1', { excludeId: 'placeholder', limit: 30 })

    expect(messages[1].content).toBe('在的。')
    expect(messages[1].content).not.toContain('⚠️')
  })

  it('断流提示同样剥掉（断 3 次就有 3 段，模型不该看到任何一段）', async () => {
    dbMock.records = [
      record({ id: 'u1', role: 'user', content: '问', createdAt: 1 }),
      record({
        id: 'a1',
        role: 'assistant',
        content:
          '半截回答\n\n> ⚠️ **连接已中断**: 回复可能不完整\n\n> ⚠️ **请求失败**: 网络已断开\n\n> ⚠️ **连接已中断**: 收到无法解析的流数据，回复可能不完整',
        createdAt: 2,
      }),
      record({ id: 'u2', role: 'user', content: '再问', createdAt: 3 }),
    ]

    const { messages } = await loadContextMessages('s1', { excludeId: 'placeholder', limit: 30 })

    expect(messages[1].content).toBe('半截回答')
  })

  it('只有提示、没有正文的失败轮回放成占位文本，而不是把提示当回答', async () => {
    dbMock.records = [
      record({ id: 'u1', role: 'user', content: '在吗', createdAt: 1 }),
      record({
        id: 'a1',
        role: 'assistant',
        content: '\n\n> ⚠️ **请求失败**: 网络已断开',
        error: { message: '网络已断开', retryable: true, code: null },
        createdAt: 2,
      }),
      record({ id: 'u2', role: 'user', content: '还在吗', createdAt: 3 }),
    ]

    const { messages } = await loadContextMessages('s1', { excludeId: 'placeholder', limit: 30 })

    expect(messages[1].content).toBe('（本轮请求失败，未产生回答）')
    expect(messages[1].content).not.toContain('⚠️')
  })

  it('模型自己在正文中间写的告警引用不动（只剥结尾的系统提示）', async () => {
    dbMock.records = [
      record({ id: 'u1', role: 'user', content: '问', createdAt: 1 }),
      record({
        id: 'a1',
        role: 'assistant',
        content: '开头说明。\n\n> ⚠️ **注意**：这样做有风险，请先备份。\n\n结尾。',
        createdAt: 2,
      }),
      record({ id: 'u2', role: 'user', content: '再问', createdAt: 3 }),
    ]

    const { messages } = await loadContextMessages('s1', { excludeId: 'placeholder', limit: 30 })

    expect(messages[1].content).toContain('⚠️ **注意**：这样做有风险')
    expect(messages[1].content).toContain('结尾。')
  })
})

describe('忽略审批之后要把 approval: null 推上去（跨设备不再拉回卡片）', () => {
  it('clearMessageApproval 落库后叫醒推送引擎', async () => {
    const { clearMessageApproval } = await import('./chat-persistence')

    await clearMessageApproval('a1')

    // 落库 `null`：字段必须显式写 null，而不是「缺席」——缺席在 push 时会被跳过，
    // 服务端那行的 approval 就永远是旧值，别的设备下次 pull 又把卡片拉回来
    expect(dbMock.update).toHaveBeenCalledWith('a1', { approval: null })
    // 表级 hook 已把本行标成 pending，但推送引擎得有人叫醒才会真的上行
    expect(pushSpy).toHaveBeenCalled()
  })
})

/**
 * 跨设备那一面：忽略后的行要能把 `approval: null` 带上去。
 *
 * 服务端按「字段非缺失」判断要不要覆盖，所以这里必须是显式的 `null` 而不是「字段缺席」。
 * 用真实的 `toMessagePushRow` 钉住这条契约，而不是只在 mock 上断言写了 `{approval: null}`。
 */
describe('忽略审批后 push payload 带的是 approval: null', () => {
  it('null 能过白名单的 undefined 判断，payload 里字段存在且为 null', async () => {
    const { toMessagePushRow } = await import('./sync/mapping')

    const row = toMessagePushRow(record({ id: 'a1', role: 'assistant', content: '答', approval: null }))

    expect('approval' in row.payload).toBe(true)
    expect(row.payload.approval).toBeNull()
  })

  it('带调用时同样带得出去（忽略前后是两个不同的明确取值）', async () => {
    const { toMessagePushRow } = await import('./sync/mapping')

    const row = toMessagePushRow(
      record({
        id: 'a1',
        role: 'assistant',
        content: '',
        approval: {
          toolCalls: [
            { toolCallId: 't1', toolName: 'rm', argsText: '{}', description: '', riskLevel: 'dangerous' },
          ],
        },
      }),
    )

    expect(row.payload.approval).toEqual({
      toolCalls: [
        { toolCallId: 't1', toolName: 'rm', argsText: '{}', description: '', riskLevel: 'dangerous' },
      ],
    })
  })
})
