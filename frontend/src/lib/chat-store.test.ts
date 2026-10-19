import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { TRANSPORT_INCOMPLETE, type ChatEvent } from '../types'
import { buildLiveMessage } from './chat-message'
import {
  activeSessionIds,
  appendNotice,
  applyEvent,
  clearClarification,
  clearFinishedRun,
  createRun,
  finishRun,
  flushNow,
  getRun,
  getRunVersion,
  getRunningSessionsKey,
  isRunActive,
  isRunPersistFailed,
  markRunPersistFailed,
  removeRun,
  subscribeRun,
  subscribeRunningSessions,
  type CreateRunParams,
} from './chat-store'

const SESSIONS = ['a', 'b', 'c']
/** store 是模块级单例，用例之间必须清干净 */
const resetStore = () => {
  for (const sessionId of [...SESSIONS, ...activeSessionIds()]) removeRun(sessionId)
  flushNow()
}

beforeEach(resetStore)
afterEach(resetStore)

const start = (sessionId = 'a', overrides: Partial<CreateRunParams> = {}) =>
  createRun({ sessionId, assistantMsgId: `m_${sessionId}`, model: 'openai/gpt-4o', ...overrides })

describe('run 生命周期', () => {
  it('同一会话同时只允许一个运行中的任务', () => {
    expect(start('a')).not.toBeNull()
    expect(start('a')).toBeNull()
    expect(isRunActive('a')).toBe(true)
  })

  it('新轮次开始前清掉同会话里已结束的旧 run', () => {
    start('a')
    applyEvent('a', { type: 'content', delta: '上一轮' })
    finishRun('a', {})

    start('a')

    // 否则新轮次会读到上一轮残留的内容
    expect(getRun('a')!.content).toBe('')
  })

  it('finishRun 结算思考耗时（只有思考没有正文的情况）', () => {
    vi.useFakeTimers()
    try {
      start('a')
      applyEvent('a', { type: 'thinking', delta: '想' })
      vi.advanceTimersByTime(120)

      expect(finishRun('a', {})!.thinkingMs).toBeGreaterThan(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('结束标记与 stopped 都落到 run 上', () => {
    start('a')
    finishRun('a', { stopped: true })

    expect(getRun('a')!.finished).toBe(true)
    expect(getRun('a')!.stopped).toBe(true)
  })

  it('未收到 done 且非主动停止 → incomplete', () => {
    start('a')
    finishRun('a', {})

    expect(getRun('a')!.incomplete).toBe(true)
  })

  it('主动停止不算 incomplete', () => {
    start('a')
    applyEvent('a', { type: 'done' })
    finishRun('a', { stopped: true })

    expect(getRun('a')!.incomplete).toBe(false)
  })

  it('收到 done 就不算 incomplete', () => {
    start('a')
    applyEvent('a', { type: 'done' })
    finishRun('a', {})

    expect(getRun('a')!.incomplete).toBe(false)
  })

  it('error 是合法终止，不算 incomplete', () => {
    start('a')
    applyEvent('a', { type: 'error', message: '上游炸了' })
    finishRun('a', {})

    // 只认 done 会把正常报错判成「连接中断」：用户先看到错误，又看到一句内容不完整
    expect(getRun('a')!.sawError).toBe(true)
    expect(getRun('a')!.incomplete).toBe(false)
  })

  it('丢过帧（damaged）时即使随后收到 done 也算 incomplete', () => {
    start('a')
    applyEvent('a', { type: TRANSPORT_INCOMPLETE, damaged: true })
    applyEvent('a', { type: 'done' })
    finishRun('a', {})

    expect(getRun('a')!.damaged).toBe(true)
    expect(getRun('a')!.incomplete).toBe(true)
  })

  it('用户主动停止不算 incomplete', () => {
    start('a')
    applyEvent('a', { type: 'error', message: '上游炸了' })
    finishRun('a', { stopped: true })

    expect(getRun('a')!.incomplete).toBe(false)
  })
})

describe('事件折叠', () => {
  it('思考与正文各自累加', () => {
    start('a')
    applyEvent('a', { type: 'thinking', delta: '想了' })
    applyEvent('a', { type: 'thinking', delta: '想' })
    applyEvent('a', { type: 'content', delta: '答' })

    const run = getRun('a')!
    expect(run.thinking).toBe('想了想')
    expect(run.content).toBe('答')
  })

  it('正文一出现就结算思考计时', () => {
    vi.useFakeTimers()
    try {
      start('a')
      applyEvent('a', { type: 'thinking', delta: '想' })
      vi.advanceTimersByTime(50)
      applyEvent('a', { type: 'content', delta: '答' })

      expect(getRun('a')!.thinkingMs).toBeGreaterThan(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('工具调用先建条目，结果到达时补齐', () => {
    start('a')
    applyEvent('a', { type: 'tool_call', tool_call_id: 'c1', tool_name: 'tavily_search', args: '{"q":1}' })

    expect(getRun('a')!.toolCalls).toEqual([
      { toolCallId: 'c1', toolName: 'tavily_search', argsText: '{"q":1}', result: undefined, status: 'running' },
    ])

    applyEvent('a', { type: 'tool_result', tool_call_id: 'c1', tool_name: 'tavily_search', result: 'ok' })

    expect(getRun('a')!.toolCalls[0]).toMatchObject({ result: 'ok', status: 'complete' })
  })

  it('结果先于调用到达时也能补上条目', () => {
    start('a')
    applyEvent('a', { type: 'tool_result', tool_call_id: 'c9', tool_name: 't', result: 'r' })

    expect(getRun('a')!.toolCalls).toEqual([
      { toolCallId: 'c9', toolName: 't', argsText: '', result: 'r', status: 'complete' },
    ])
  })

  it('clarification 事件落成前端形状', () => {
    start('a')
    applyEvent('a', {
      type: 'clarification',
      tool_call_id: 'ask-1',
      question: '选哪个？',
      options: ['A', 'B'],
      allow_custom_input: false,
    })

    expect(getRun('a')!.clarification).toEqual({
      toolCallId: 'ask-1',
      question: '选哪个？',
      options: ['A', 'B'],
      allowCustomInput: false,
    })
  })

  it('fallback 事件记录模型切换，且不写进正文', () => {
    start('a')
    expect(
      applyEvent('a', { type: 'fallback', from_model: 'openai/gpt-4o', to_model: 'openai/gpt-4o-mini' }),
    ).toBe(true)
    applyEvent('a', { type: 'content', delta: '答' })

    const run = getRun('a')!
    expect(run.fallback).toEqual({ fromModel: 'openai/gpt-4o', toModel: 'openai/gpt-4o-mini' })
    // 写进正文会被当成助手历史再次发给模型，也会被复制/导出当成回答的一部分
    expect(run.content).toBe('答')
  })

  it('error 事件追加可见提示', () => {
    start('a')
    applyEvent('a', { type: 'error', message: '上游炸了' })

    expect(getRun('a')!.content).toContain('**错误**: 上游炸了')
  })

  it('error 事件记录服务端给出的 retryable，供「重试」按钮判断', () => {
    start('a')
    applyEvent('a', { type: 'error', message: '上游炸了', retryable: true })

    expect(getRun('a')!.error).toEqual({ message: '上游炸了', retryable: true, code: null })
  })

  it('旧服务端不返回 retryable 时按不可重试处理', () => {
    start('a')
    applyEvent('a', { type: 'error', message: '校验失败' })

    expect(getRun('a')!.error).toEqual({ message: '校验失败', retryable: false, code: null })
  })

  it('上下文超限的提示换标签，不和一般故障混为一类', () => {
    start('a')
    // 服务端文案里带用量与动作，前端不再自己拼一遍（重复拼接会两处漂移）
    applyEvent('a', {
      type: 'error',
      message: '本会话上下文约 900,000 tokens，超出上限……请新建会话后继续提问',
      retryable: false,
      code: 'context_overflow',
    })

    expect(getRun('a')!.content).toContain('**上下文已满**:')
    expect(getRun('a')!.content).not.toContain('**错误**:')
    expect(getRun('a')!.error?.code).toBe('context_overflow')
  })

  it('未知的 code 不落进 run（不让协议外的字符串影响渲染判断）', () => {
    start('a')
    const run = getRun('a')!
    applyEvent('a', {
      type: 'error',
      message: '出错了',
      // 模拟更新的服务端发了本版前端还认不出的分类
      code: 'something_new' as unknown as undefined,
    })

    expect(run.content).toContain('**错误**:')
    expect(run.error?.code).toBeNull()
  })

  it('usage 事件记录真实用量，且不写进正文', () => {
    start('a')
    expect(
      applyEvent('a', {
        type: 'usage',
        context_tokens: 1066,
        context_window: 1048000,
        output_tokens: 2,
        cache_read_tokens: 896,
      }),
    ).toBe(true)
    applyEvent('a', { type: 'content', delta: '答' })

    const run = getRun('a')!
    expect(run.usage).toEqual({
      contextTokens: 1066,
      contextWindow: 1048000,
      outputTokens: 2,
      cacheReadTokens: 896,
    })
    // 与 fallback 同一条约束：这一行是元信息，不属于回答正文
    expect(run.content).toBe('答')
  })

  it('用量里的窗口缺失（网关没报）时按 null 记，而不是编一个数', () => {
    start('a')
    applyEvent('a', {
      type: 'usage',
      context_tokens: 1066,
      context_window: null,
      output_tokens: 2,
      cache_read_tokens: 0,
    })

    expect(getRun('a')!.usage?.contextWindow).toBeNull()
  })

  it('INCOMPLETE 追加中断提示并标记不完整', () => {
    start('a')
    applyEvent('a', { type: TRANSPORT_INCOMPLETE, damaged: false })

    expect(getRun('a')!.incomplete).toBe(true)
    expect(getRun('a')!.content).toContain('连接已中断')
  })

  it('damaged 的 INCOMPLETE 提示里点明收到无法解析的数据', () => {
    start('a')
    applyEvent('a', { type: TRANSPORT_INCOMPLETE, damaged: true })

    expect(getRun('a')!.content).toContain('无法解析的流数据')
  })

  it('未知事件被忽略且不触发渲染', () => {
    start('a')
    flushNow()
    const listener = vi.fn()
    subscribeRun('a', listener)

    // 模拟服务端未来新增的事件类型：客户端契约里没有它
    expect(applyEvent('a', { type: '未来新事件' } as unknown as ChatEvent)).toBe(false)
    flushNow()

    expect(listener).not.toHaveBeenCalled()
  })

  it('done 不触发渲染（只是内部标记）', () => {
    start('a')
    flushNow()
    const listener = vi.fn()
    subscribeRun('a', listener)

    expect(applyEvent('a', { type: 'done' })).toBe(false)
    flushNow()

    expect(listener).not.toHaveBeenCalled()
  })

  it('没有 run 时折叠事件是空操作', () => {
    expect(applyEvent('nope', { type: 'content', delta: 'x' })).toBe(false)
  })
})

describe('partOrder（内容块到达顺序）', () => {
  it('按事件到达顺序记录：思考 → 工具 → 正文', () => {
    start('a')
    applyEvent('a', { type: 'thinking', delta: '想了' })
    applyEvent('a', { type: 'tool_call', tool_call_id: 'c1', tool_name: 'tavily_search', args: '{}' })
    applyEvent('a', { type: 'content', delta: '答' })

    expect(getRun('a')!.partOrder).toEqual(['reasoning', 'tool-call', 'text'])
  })

  it('同一类块的后续增量不重复占位', () => {
    start('a')
    applyEvent('a', { type: 'thinking', delta: '想' })
    applyEvent('a', { type: 'thinking', delta: '了' })
    applyEvent('a', { type: 'content', delta: '答' })
    applyEvent('a', { type: 'content', delta: '案' })

    expect(getRun('a')!.partOrder).toEqual(['reasoning', 'text'])
  })

  it('每个新工具调用各占一位：同名不同 id 不合并', () => {
    start('a')
    applyEvent('a', { type: 'tool_call', tool_call_id: 'c1', tool_name: 'tavily_search', args: '{}' })
    applyEvent('a', { type: 'tool_call', tool_call_id: 'c2', tool_name: 'tavily_search', args: '{}' })

    // 两个块：只占一位会让第二个调用被补到末尾，画错与正文的先后关系
    expect(getRun('a')!.partOrder).toEqual(['tool-call', 'tool-call'])
  })

  it('同一 id 的 tool_call 重发不重复占位', () => {
    start('a')
    applyEvent('a', { type: 'tool_call', tool_call_id: 'c1', tool_name: 't', args: '{}' })
    applyEvent('a', { type: 'tool_call', tool_call_id: 'c1', tool_name: 't', args: '{}' })

    expect(getRun('a')!.partOrder).toEqual(['tool-call'])
  })

  it('空增量不占位（没有内容的块不该出现在顺序表里）', () => {
    start('a')
    applyEvent('a', { type: 'thinking', delta: '' })
    applyEvent('a', { type: 'content', delta: '' })

    expect(getRun('a')!.partOrder).toEqual([])
  })

  it('与 chat-message 对接：渲染出的部件顺序等于事件到达顺序', () => {
    start('a')
    applyEvent('a', { type: 'tool_call', tool_call_id: 'c1', tool_name: 't', args: '{}' })
    applyEvent('a', { type: 'content', delta: '中间的话' })
    applyEvent('a', { type: 'tool_call', tool_call_id: 'c2', tool_name: 't', args: '{}' })

    const parts = buildLiveMessage(getRun('a')!).content as unknown as Array<{ type: string }>

    // 按类型分组会把工具全顶到最前面，这里锁住「工具夹在正文两侧」的还原能力
    expect(parts.map((part) => part.type)).toEqual(['tool-call', 'text', 'tool-call'])
  })
})

describe('appendNotice', () => {
  it('带 detail 时格式为 label: detail', () => {
    start('a')
    appendNotice('a', '请求失败', 'HTTP 500')

    expect(getRun('a')!.content).toBe('\n\n> ⚠️ **请求失败**: HTTP 500')
  })

  it('不带 detail 时只有 label', () => {
    start('a')
    appendNotice('a', '连接已中断')

    expect(getRun('a')!.content).toBe('\n\n> ⚠️ **连接已中断**')
  })
})

describe('订阅粒度（O-1）', () => {
  it('会话 A 的订阅者不会因会话 B 的增量而触发', () => {
    start('a')
    start('b')
    flushNow()

    const listenerA = vi.fn()
    subscribeRun('a', listenerA)

    applyEvent('b', { type: 'content', delta: 'x' })
    flushNow()

    expect(listenerA).not.toHaveBeenCalled()
  })

  it('自己的增量会触发自己的订阅者', () => {
    start('a')
    flushNow()

    const listenerA = vi.fn()
    subscribeRun('a', listenerA)

    applyEvent('a', { type: 'content', delta: 'x' })
    flushNow()

    expect(listenerA).toHaveBeenCalledTimes(1)
  })

  it('一次网络分片里的多个增量合并成一次渲染', () => {
    start('a')
    flushNow()

    const listener = vi.fn()
    subscribeRun('a', listener)

    applyEvent('a', { type: 'content', delta: '1' })
    applyEvent('a', { type: 'content', delta: '2' })
    applyEvent('a', { type: 'content', delta: '3' })
    flushNow()

    expect(listener).toHaveBeenCalledTimes(1)
  })

  it('退订后不再收到通知', () => {
    start('a')
    flushNow()

    const listener = vi.fn()
    const unsubscribe = subscribeRun('a', listener)
    unsubscribe()

    applyEvent('a', { type: 'content', delta: 'x' })
    flushNow()

    expect(listener).not.toHaveBeenCalled()
  })

  it('getRunVersion 按会话独立递增', () => {
    start('a')
    start('b')
    flushNow()
    const beforeA = getRunVersion('a')
    const beforeB = getRunVersion('b')

    applyEvent('a', { type: 'content', delta: 'x' })
    flushNow()

    // 版本号来自全局单调计数器（不回退），因此只断言「自己变大、别人不动」
    expect(getRunVersion('a')).toBeGreaterThan(beforeA)
    expect(getRunVersion('b')).toBe(beforeB)
  })

  it('run 被移除后版本号继续递增（同值不会触发重渲染）', () => {
    start('a')
    flushNow()
    const before = getRunVersion('a')
    expect(before).toBeGreaterThan(0)

    removeRun('a')
    flushNow()
    const afterRemove = getRunVersion('a')
    // 旧实现删掉版本记录后从 1 重新开始：快照没变 → useSyncExternalStore 不重渲染，
    // 界面会停在已经被删除的 run 上
    expect(afterRemove).toBeGreaterThan(before)

    start('a')
    flushNow()
    expect(getRunVersion('a')).toBeGreaterThan(afterRemove)
  })

  it('未订阅的通知不会打扰 running-sessions 订阅者（流式增量不算集合变化）', () => {
    start('a')
    flushNow()

    const listener = vi.fn()
    subscribeRunningSessions(listener)

    applyEvent('a', { type: 'content', delta: 'x' })
    flushNow()

    expect(listener).not.toHaveBeenCalled()
  })

  it('running-sessions 订阅者只在集合真正变化时触发', () => {
    const listener = vi.fn()
    subscribeRunningSessions(listener)

    start('a')
    flushNow()
    expect(listener).toHaveBeenCalledTimes(1)
    expect(getRunningSessionsKey()).toBe('a')

    finishRun('a', {})
    flushNow()
    expect(listener).toHaveBeenCalledTimes(2)
    expect(getRunningSessionsKey()).toBe('')

    // 空集合上再做无效变更，不该再通知
    applyEvent('a', { type: 'content', delta: 'x' })
    flushNow()
    expect(listener).toHaveBeenCalledTimes(2)
  })

  it('running-sessions 快照是排序稳定的字符串', () => {
    start('a')
    start('b')
    flushNow()

    expect(getRunningSessionsKey()).toBe('a,b')

    finishRun('a', {})
    flushNow()

    expect(getRunningSessionsKey()).toBe('b')
  })
})

describe('清理', () => {
  it('已结束且无待答提问 → 可以丢弃内存态', () => {
    start('a')
    finishRun('a', {})
    clearFinishedRun('a')
    flushNow()

    expect(getRun('a')).toBeNull()
  })

  it('仍有待答提问时保留内存态（否则浮层失去依托）', () => {
    start('a')
    // allow_custom_input 是线上契约的必填字段；缺省时 applyEvent 同样按 true 处理
    applyEvent('a', { type: 'clarification', tool_call_id: 'c', question: '选？', options: [], allow_custom_input: true })
    finishRun('a', {})

    clearFinishedRun('a')
    flushNow()

    expect(getRun('a')).not.toBeNull()
  })

  it('忽略提问后内存态才真正释放', () => {
    start('a')
    applyEvent('a', { type: 'clarification', tool_call_id: 'c', question: '选？', options: [], allow_custom_input: true })
    finishRun('a', {})

    clearClarification('a')
    clearFinishedRun('a')
    flushNow()

    expect(getRun('a')).toBeNull()
  })

  it('生成中的 run 不会被丢弃', () => {
    start('a')
    clearFinishedRun('a')
    flushNow()

    expect(isRunActive('a')).toBe(true)
  })
})

describe('落库失败（persistFailed）', () => {
  it('标记后 clearFinishedRun 不再丢弃内存态', () => {
    start('a')
    applyEvent('a', { type: 'content', delta: '刚流出的回复' })
    applyEvent('a', { type: 'done' })
    markRunPersistFailed('a')
    finishRun('a', {})

    clearFinishedRun('a')
    flushNow()

    // DB 里还是旧内容：丢掉内存态等于用旧内容替换刚流出的回复
    expect(getRun('a')!.content).toBe('刚流出的回复')
    expect(isRunPersistFailed('a')).toBe(true)
  })

  it('重复标记只返回一次 true（调用方据此只报错一次）', () => {
    start('a')

    expect(markRunPersistFailed('a')).toBe(true)
    expect(markRunPersistFailed('a')).toBe(false)
  })

  it('没有 run 时标记与查询都是空操作', () => {
    expect(markRunPersistFailed('nope')).toBe(false)
    expect(isRunPersistFailed('nope')).toBe(false)
  })
})
