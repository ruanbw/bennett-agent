import { describe, expect, it } from 'vitest'

import type {
  ClarificationState,
  MessageCustomMetadata,
  Run,
  RuntimeMessage,
  RuntimePart,
  ToolCallRecord,
} from '../types'
import { buildLiveMessage, buildParts, pendingClarification, toRuntimeMessage } from './chat-message'

/**
 * 断言用：content 部件是判别联合（status / toolCallId 只属于部分分支），
 * 且 `ThreadMessageLike['content']` 还带一个 string 分支。用例只断言其中几个字段，
 * 这里统一收窄一次，免得逐处写 Extract<> / 非空断言。
 */
type AssertedPart = {
  type: string
  status: { type: string }
  toolCallId: string
  toolName: string
}

const assertParts = (parts: RuntimePart[]): AssertedPart[] => parts as unknown as AssertedPart[]

/** runtime 消息同理：content 含 string 分支，status / metadata 都是可选的。 */
type AssertedMessage = {
  content: AssertedPart[]
  status?: { type: string; reason?: string }
  createdAt: Date
  metadata: { custom: MessageCustomMetadata }
}

const assertMessage = (message: RuntimeMessage): AssertedMessage => message as unknown as AssertedMessage

/**
 * 用例会刻意传入不完整的 clarification（只有 question / options）验证健壮性，
 * 因此 overrides 放宽后再在返回处断言成契约形状。
 */
type RunOverrides = Partial<Omit<Run, 'clarification'>> & {
  clarification?: Partial<ClarificationState> | null
}

const run = (overrides: RunOverrides = {}): Run =>
  ({
    sessionId: 's1',
    assistantMsgId: 'm1',
    startedAt: 1_700_000_000_000,
    thinking: '',
    content: '',
    thinkingMs: 0,
    toolCalls: [],
    clarification: null,
    stopped: false,
    finished: false,
    ...overrides,
  }) as Run

describe('buildParts', () => {
  it('按工具 → 思考 → 正文的顺序组装', () => {
    const parts = assertParts(
      buildParts({
        toolCalls: [{ toolCallId: 'c1', toolName: 'tavily_search', argsText: '{}', status: 'complete' }],
        thinking: '想了想',
        content: '答案',
        reasoningStatus: 'complete',
        textStatus: 'complete',
      }),
    )

    expect(parts.map((p) => p.type)).toEqual(['tool-call', 'reasoning', 'text'])
  })

  it('空内容不产出空 part', () => {
    expect(buildParts({ reasoningStatus: 'complete', textStatus: 'complete' })).toEqual([])
  })

  it('status 由调用方决定：已完成与生成中共用同一段组装逻辑', () => {
    const streaming = assertParts(
      buildParts({
        thinking: 'x',
        content: 'y',
        reasoningStatus: 'running',
        textStatus: 'running',
      }),
    )
    expect(streaming.map((p) => p.status.type)).toEqual(['running', 'running'])

    const done = assertParts(
      buildParts({ thinking: 'x', content: 'y', reasoningStatus: 'complete', textStatus: 'complete' }),
    )
    expect(done.map((p) => p.status.type)).toEqual(['complete', 'complete'])
  })

  it('工具调用缺少 id 时兜底一个，避免 React key 为 undefined', () => {
    const [part] = assertParts(
      buildParts({
        toolCalls: [{ toolName: 't' }] as ToolCallRecord[],
        reasoningStatus: 'complete',
        textStatus: 'complete',
      }),
    )

    expect(typeof part.toolCallId).toBe('string')
    expect(part.toolCallId.length).toBeGreaterThan(0)
  })

  it('过滤历史记录里的内部工具（旧数据兼容）', () => {
    const parts = assertParts(
      buildParts({
        toolCalls: [
          { toolCallId: 'c1', toolName: 'ask_user_clarification', argsText: '{}' },
          { toolCallId: 'c2', toolName: 'tavily_search', argsText: '{}' },
        ] as ToolCallRecord[],
        reasoningStatus: 'complete',
        textStatus: 'complete',
      }),
    )

    expect(parts.map((p) => p.toolName)).toEqual(['tavily_search'])
  })
})

describe('toRuntimeMessage', () => {
  it('已完成记录的思考与正文都是 complete', () => {
    const message = assertMessage(
      toRuntimeMessage({
        id: 'm1',
        sessionId: 's1',
        role: 'assistant',
        content: '答案',
        thinking: '想了想',
        createdAt: 1,
      }),
    )

    expect(message.content.map((p) => p.status.type)).toEqual(['complete', 'complete'])
    expect(message.status).toEqual({ type: 'complete', reason: 'stop' })
    expect(message.createdAt).toBeInstanceOf(Date)
  })

  it('缺少可选字段时不炸', () => {
    const message = assertMessage(
      toRuntimeMessage({ id: 'm1', sessionId: 's1', role: 'assistant', content: '', createdAt: 1 }),
    )

    expect(message.content).toEqual([])
    expect(message.metadata.custom).toEqual({ stopped: false, thinkingMs: 0, clarification: null })
  })

  it('用户消息不带 status', () => {
    expect(
      toRuntimeMessage({ id: 'm1', sessionId: 's1', role: 'user', content: 'hi', createdAt: 1 }).status,
    ).toBeUndefined()
  })
})

describe('buildLiveMessage', () => {
  it('生成中且只有思考时：思考 running', () => {
    const message = assertMessage(buildLiveMessage(run({ thinking: '想' })))

    expect(message.content.map((p) => p.status.type)).toEqual(['running'])
    expect(message.status).toBeUndefined()
  })

  it('正文一出现，思考即视为结束（与主流平台一致）', () => {
    const message = assertMessage(buildLiveMessage(run({ thinking: '想', content: '答' })))

    expect(message.content.map((p) => p.status.type)).toEqual(['complete', 'running'])
  })

  it('本轮结束后正文标记 complete', () => {
    const message = assertMessage(buildLiveMessage(run({ content: '答', finished: true })))

    expect(message.status).toEqual({ type: 'complete', reason: 'stop' })
    expect(message.content[0].status.type).toBe('complete')
  })

  it('生成中不呈现待答提问，避免用户没看清就被下一段增量挤走', () => {
    const message = assertMessage(
      buildLiveMessage(run({ clarification: { question: '选哪个？', options: ['A'] }, finished: false })),
    )

    expect(message.metadata.custom.clarification).toBeNull()
  })

  it('本轮结束后才呈现待答提问', () => {
    const clarification = { question: '选哪个？', options: ['A', 'B'], allowCustomInput: true }
    const message = assertMessage(buildLiveMessage(run({ clarification, finished: true })))

    expect(message.metadata.custom.clarification).toEqual(clarification)
  })
})

describe('pendingClarification（R-6：从消息列表推导，不用 handled 标记）', () => {
  const assistantWith = (clarification: Partial<ClarificationState> | null): RuntimeMessage => ({
    id: 'm1',
    role: 'assistant',
    content: '',
    metadata: { custom: { clarification } },
  })

  it('最后一条是带提问的助手消息 → 待答', () => {
    const messages = [assistantWith({ question: '选哪个？', options: ['A'] })]

    expect(pendingClarification(messages, false)).toEqual({
      messageId: 'm1',
      question: '选哪个？',
      options: ['A'],
    })
  })

  it('用户回答后追加了 user 消息 → 自动不再待答', () => {
    const messages: RuntimeMessage[] = [
      assistantWith({ question: '选哪个？' }),
      { id: 'm2', role: 'user', content: '' },
    ]

    expect(pendingClarification(messages, false)).toBeNull()
  })

  it('用户忽略后记录上的 clarification 被清空 → 自动不再待答', () => {
    expect(pendingClarification([assistantWith(null)], false)).toBeNull()
  })

  it('没有 clarification 字段 → 不待答', () => {
    expect(pendingClarification([{ id: 'm1', role: 'assistant', content: '', metadata: {} }], false)).toBeNull()
  })

  it('生成中一律不待答', () => {
    const messages = [assistantWith({ question: '选哪个？' })]

    expect(pendingClarification(messages, true)).toBeNull()
  })

  it('空列表不炸', () => {
    expect(pendingClarification([], false)).toBeNull()
    expect(pendingClarification(undefined, false)).toBeNull()
  })

  it('只有 question 非空的才算提问', () => {
    expect(pendingClarification([assistantWith({ options: ['A'] })], false)).toBeNull()
  })
})
