import { describe, expect, it } from 'vitest'

import type {
  ClarificationState,
  MessageCustomMetadata,
  MessageRecord,
  Run,
  RuntimeMessage,
  RuntimePart,
  ToolCallRecord,
} from '../types'
import {
  buildLiveMessage,
  buildParts,
  contextExhausted,
  pendingClarification,
  toRuntimeMessage,
  type PartKind,
} from './chat-message'

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
  /** 工具耗时：运行时读 `timing.completedAt - timing.startedAt`（见 useToolCallElapsed） */
  timing?: { startedAt: number; completedAt?: number }
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
  partOrder?: PartKind[]
}

const run = (overrides: RunOverrides = {}): Run & { partOrder?: PartKind[] } =>
  ({
    sessionId: 's1',
    assistantMsgId: 'm1',
    startedAt: 1_700_000_000_000,
    thinking: '',
    content: '',
    thinkingMs: 0,
    toolCalls: [],
    clarification: null,
    fallback: null,
    error: null,
    stopped: false,
    finished: false,
    ...overrides,
  }) as Run

describe('buildParts', () => {
  it('没有到达顺序记录时按「思考 → 工具 → 正文」组装', () => {
    const parts = assertParts(
      buildParts({
        toolCalls: [{ toolCallId: 'c1', toolName: 'tavily_search', argsText: '{}', status: 'complete' }],
        thinking: '想了想',
        content: '答案',
        reasoningStatus: 'complete',
        textStatus: 'complete',
      }),
    )

    // 本后端的事件流是「先思考、再工具、最后正文」，把工具顶到最前是对顺序的臆造
    expect(parts.map((p) => p.type)).toEqual(['reasoning', 'tool-call', 'text'])
  })

  it('有到达顺序时照办（思考与工具按到达顺序），但正文整块钉在最后', () => {
    const parts = assertParts(
      buildParts({
        toolCalls: [
          { toolCallId: 'c1', toolName: 'tavily_search', argsText: '{}' },
          { toolCallId: 'c2', toolName: 'tavily_search', argsText: '{}' },
        ] as ToolCallRecord[],
        thinking: '想了想',
        content: '答案',
        order: ['reasoning', 'tool-call', 'text', 'tool-call'],
        reasoningStatus: 'complete',
        textStatus: 'complete',
      }),
    )

    // 顺序表里 text 夹在两个调用中间，但正文只有一个块、无法拆开去夹；整块排在最后
    expect(parts.map((p) => p.type)).toEqual(['reasoning', 'tool-call', 'tool-call', 'text'])
    expect(parts.filter((p) => p.type === 'tool-call').map((p) => p.toolCallId)).toEqual(['c1', 'c2'])
  })

  it('顺序表被写坏（未知项、遗漏）也不吞内容', () => {
    const parts = assertParts(
      buildParts({
        toolCalls: [{ toolCallId: 'c1', toolName: 'tavily_search', argsText: '{}' }] as ToolCallRecord[],
        thinking: '想',
        content: '答',
        order: ['unknown' as PartKind, 'text'],
        reasoningStatus: 'complete',
        textStatus: 'complete',
      }),
    )

    expect(parts.map((p) => p.type)).toEqual(['reasoning', 'tool-call', 'text'])
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
    expect(message.metadata.custom).toEqual({
      stopped: false,
      thinkingMs: 0,
      clarification: null,
      // 老记录没有待审批调用：呈现为 null，审批卡片据此不出现
      approval: null,
      fallback: null,
      compaction: null,
      error: null,
      // 老记录没有 model / usage：呈现为零值而不是 undefined，否则 metadata 形状随记录而变
      model: null,
      usage: null,
      incomplete: false,
    })
  })

  it('把本轮的模型带进 metadata.custom（上下文锁靠它区分模型）', () => {
    const base: MessageRecord = { id: 'm1', sessionId: 's1', role: 'assistant', content: '答', createdAt: 1 }

    expect(assertMessage(toRuntimeMessage({ ...base, model: 'group/auto-tiny' })).metadata.custom.model).toBe(
      'group/auto-tiny',
    )
    expect(assertMessage(buildLiveMessage(run({ model: 'group/auto-tiny' }))).metadata.custom.model).toBe(
      'group/auto-tiny',
    )
  })

  it('用户消息不带 status', () => {
    expect(
      toRuntimeMessage({ id: 'm1', sessionId: 's1', role: 'user', content: 'hi', createdAt: 1 }).status,
    ).toBeUndefined()
  })

  it('把记录上的 incomplete 映射进 metadata.custom（供 thread 渲染「不完整」）', () => {
    const base: MessageRecord = { id: 'm1', sessionId: 's1', role: 'assistant', content: '半截', createdAt: 1 }

    expect(assertMessage(toRuntimeMessage({ ...base, incomplete: true })).metadata.custom.incomplete).toBe(true)
    // 字段缺失（老记录）时行为不变：没有标记
    expect(assertMessage(toRuntimeMessage(base)).metadata.custom.incomplete).toBe(false)
  })

  it('把记录上的 fallback 映射进 metadata.custom（刷新后仍能显示切换提示）', () => {
    const base: MessageRecord = { id: 'm1', sessionId: 's1', role: 'assistant', content: '答', createdAt: 1 }
    const fallback = { fromModel: 'openai/gpt-4o', toModel: 'openai/gpt-4o-mini' }

    expect(assertMessage(toRuntimeMessage(base)).metadata.custom.fallback).toBeNull()
    expect(assertMessage(toRuntimeMessage({ ...base, fallback })).metadata.custom.fallback).toEqual(fallback)
  })

  it('把记录上的 error 映射进 metadata.custom（刷新后重试按钮仍可用）', () => {
    const base: MessageRecord = { id: 'm1', sessionId: 's1', role: 'assistant', content: '半截', createdAt: 1 }
    const error = { message: '服务暂时不可用，请重试', retryable: true, code: null }

    expect(assertMessage(toRuntimeMessage(base)).metadata.custom.error).toBeNull()
    expect(assertMessage(toRuntimeMessage({ ...base, error })).metadata.custom.error).toEqual(error)
  })

  it('失败分类随记录一起还原（刷新后仍能区分「上下文已满」与一般错误）', () => {
    const base: MessageRecord = { id: 'm1', sessionId: 's1', role: 'assistant', content: '半截', createdAt: 1 }
    const error = { message: '本会话上下文超出上限', retryable: false, code: 'context_overflow' as const }

    expect(assertMessage(toRuntimeMessage({ ...base, error })).metadata.custom.error).toEqual(error)
  })
})

describe('buildLiveMessage', () => {
  it('生成中且只有思考时：思考 running', () => {
    const message = assertMessage(buildLiveMessage(run({ thinking: '想' })))

    expect(message.content.map((p) => p.status.type)).toEqual(['running'])
    expect(message.status).toBeUndefined()
  })

  it('生成中的消息带思考开始时间，历史消息不带', () => {
    const live = assertMessage(buildLiveMessage(run({ thinking: '想', thinkingStartedAt: 1_700_000_001_234 })))
    expect(live.metadata.custom.thinkingStartedAt).toBe(1_700_000_001_234)

    const history = assertMessage(
      toRuntimeMessage({ id: 'm1', sessionId: 's1', role: 'assistant', content: '答', createdAt: 1 }),
    )
    expect(history.metadata.custom.thinkingStartedAt).toBeUndefined()
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

  it('live 覆盖层与落库记录给出同一条 incomplete 语义', () => {
    expect(assertMessage(buildLiveMessage(run())).metadata.custom.incomplete).toBe(false)
    expect(assertMessage(buildLiveMessage(run({ incomplete: true }))).metadata.custom.incomplete).toBe(true)
  })

  it('切换提示在生成中就映射进 metadata.custom，不等本轮结束', () => {
    const fallback = { fromModel: 'openai/gpt-4o', toModel: 'openai/gpt-4o-mini' }

    expect(assertMessage(buildLiveMessage(run({ fallback, finished: false }))).metadata.custom.fallback).toEqual(
      fallback,
    )
  })

  it('失败信息在生成中/结束后都带 retryable，按钮不必等落库', () => {
    const error = { message: '生成异常，请重试', retryable: true, code: null }

    expect(assertMessage(buildLiveMessage(run({ error, finished: false }))).metadata.custom.error).toEqual(error)
    expect(assertMessage(buildLiveMessage(run({ error, finished: true }))).metadata.custom.error).toEqual(error)
  })
})

describe('事件到达顺序（buildLiveMessage / toRuntimeMessage 一致）', () => {
  const toolCalls: ToolCallRecord[] = [
    { toolCallId: 'c1', toolName: 'tavily_search', argsText: '{}', status: 'complete' },
  ]
  // 工具先于思考到达：按类型分组会把它画成「思考 → 工具」，与事件流不符
  const partOrder: PartKind[] = ['tool-call', 'reasoning', 'text']
  const record: MessageRecord & { partOrder?: PartKind[] } = {
    id: 'm1',
    sessionId: 's1',
    role: 'assistant',
    content: '答案',
    thinking: '想了想',
    toolCalls,
    createdAt: 1,
    partOrder,
  }

  it('toRuntimeMessage 照记录的顺序排布', () => {
    const message = assertMessage(toRuntimeMessage(record))

    expect(message.content.map((p) => p.type)).toEqual(['tool-call', 'reasoning', 'text'])
  })

  it('正文永远排在最后：先吐半句开场白再调工具，也不能把答案顶到过程组上面', () => {
    const earlyText: PartKind[] = ['text', 'tool-call', 'reasoning']

    const message = assertMessage(
      toRuntimeMessage({
        ...record,
        partOrder: earlyText,
      } as MessageRecord & { partOrder?: PartKind[] }),
    )

    expect(message.content.map((p) => p.type)).toEqual(['tool-call', 'reasoning', 'text'])
  })

  it('buildLiveMessage 与 toRuntimeMessage 顺序一致', () => {
    const live = assertMessage(
      buildLiveMessage(
        run({ thinking: '想了想', content: '答案', toolCalls, partOrder, finished: true }),
      ),
    )

    expect(live.content.map((p) => p.type)).toEqual(
      assertMessage(toRuntimeMessage(record)).content.map((p) => p.type),
    )
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

describe('contextExhausted（上下文已满 → 锁住输入框）', () => {
  const overflowing = (model: string | null = 'group/auto-tiny'): RuntimeMessage => ({
    id: 'm1',
    role: 'assistant',
    content: '',
    metadata: { custom: { model, error: { message: '上下文已满', retryable: false, code: 'context_overflow' } } },
  })

  it('最后一条是上下文溢出的助手消息 → 锁', () => {
    expect(contextExhausted([overflowing()], 'group/auto-tiny')).toBe(true)
  })

  it('换成别的模型 → 解锁（提示文案说的另一条出路）', () => {
    // 同一份历史在 32k 窗口上超限、在 1M 窗口上没问题，所以锁必须跟着模型走
    expect(contextExhausted([overflowing('group/auto-tiny')], 'group/auto-mega')).toBe(false)
  })

  it('拿不到模型 id（老记录）→ 一律锁', () => {
    // 宁可多拦一次，也不要让用户对着一个必然失败的会话反复点发送
    expect(contextExhausted([overflowing(null)], 'group/auto-mega')).toBe(true)
  })

  it('用户再次提问后（最后一条是 user）→ 不再锁', () => {
    // 与 pendingClarification 同一条推理：新消息一到，推导条件自然不成立
    const messages: RuntimeMessage[] = [overflowing(), { id: 'm2', role: 'user', content: '再问' }]

    expect(contextExhausted(messages, 'group/auto-tiny')).toBe(false)
  })

  it('其它失败（超时/5xx）不锁', () => {
    const other: RuntimeMessage = {
      id: 'm1',
      role: 'assistant',
      content: '',
      metadata: { custom: { model: 'group/auto-tiny', error: { message: '生成超时', retryable: true, code: null } } },
    }

    expect(contextExhausted([other], 'group/auto-tiny')).toBe(false)
  })

  it('没有失败信息 / 空列表 / undefined 都不锁', () => {
    expect(contextExhausted([{ id: 'm1', role: 'assistant', content: '', metadata: {} }], 'm')).toBe(false)
    expect(contextExhausted([], 'm')).toBe(false)
    expect(contextExhausted(undefined, 'm')).toBe(false)
  })
})

describe('工具耗时（timing）', () => {
  const record = (extra: Partial<ToolCallRecord> = {}): ToolCallRecord => ({
    toolCallId: 'c1',
    toolName: 'tavily_search',
    argsText: '{}',
    status: 'complete',
    result: 'ok',
    ...extra,
  });

  it('落库的起止时间会进到部件的 timing —— 耗时 chip 靠它', () => {
    const parts = assertParts(
      buildParts({
        toolCalls: [record({ startedAt: 1_000, finishedAt: 3_500 })],
        reasoningStatus: 'complete',
        textStatus: 'complete',
      }),
    );

    const tool = parts.find((part) => part.type === 'tool-call');
    expect(tool?.timing).toEqual({ startedAt: 1_000, completedAt: 3_500 });
  });

  it('还在跑（只有起点）时不传 completedAt，chip 会按运行中计时', () => {
    const parts = assertParts(
      buildParts({
        toolCalls: [record({ status: 'running', result: undefined, startedAt: 1_000 })],
        reasoningStatus: 'complete',
        textStatus: 'complete',
      }),
    );

    const tool = parts.find((part) => part.type === 'tool-call');
    expect(tool?.timing).toEqual({ startedAt: 1_000, completedAt: undefined });
  });

  it('老记录没有时间戳就不传 timing（与改动前一致：不显示耗时，也不编一个 0）', () => {
    const parts = assertParts(
      buildParts({
        toolCalls: [record()],
        reasoningStatus: 'complete',
        textStatus: 'complete',
      }),
    );

    const tool = parts.find((part) => part.type === 'tool-call');
    expect(tool?.timing).toBeUndefined();
  });
});
