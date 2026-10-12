import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ChatEvent, MessageRecord } from '../types'
import { abortRun, activeSessionIds, clearFinishedRun, flushNow, getRun, removeRun } from './chat-store'
import {
  flushAllRunsNow,
  registerLifecycleFlush,
  requestSessionMessagesReload,
  startChatRun,
} from './chat-runs'

/**
 * chat-runs 的两块依赖都在这里替换掉：
 *   - db：IndexedDB 在 node 环境不存在，saveProgress/saveFinal 直接断言调用形状；
 *   - chat-transport：用可控的事件流驱动 run 状态机，不碰 fetch/SSE。
 */
const mocks = vi.hoisted(() => ({
  update: vi.fn(async (_id: string, _changes: Record<string, unknown>) => 1),
  sessionUpdate: vi.fn(async (_id: string, _changes: Record<string, unknown>) => 1),
  streamChat: vi.fn(),
  /** loadSessionMessages 读回的记录（真实实现按 createdAt 升序）。 */
  records: [] as MessageRecord[],
}))

vi.mock('../db', () => ({
  db: {
    messages: {
      update: mocks.update,
      where: vi.fn(() => ({ equals: vi.fn(() => ({ sortBy: async () => mocks.records })) })),
    },
    sessions: { update: mocks.sessionUpdate },
  },
}))

vi.mock('./chat-transport', () => ({ streamChat: mocks.streamChat }))

/** 吐完预设事件后一直挂着：模拟「仍在生成」。 */
function pendingStream(...events: ChatEvent[]): void {
  const gate = new Promise<void>(() => {})
  mocks.streamChat.mockImplementation(async function* () {
    for (const event of events) yield event
    await gate
  })
}

/** 吐完预设事件就结束：配合 done 事件模拟「干净完成」。 */
function finishingStream(...events: ChatEvent[]): void {
  mocks.streamChat.mockImplementation(async function* () {
    for (const event of events) yield event
  })
}

const start = (): void => {
  startChatRun({ sessionId: 'a', assistantMsgId: 'm_a', model: null, reasoningEffort: null })
}

beforeEach(() => {
  mocks.update.mockReset().mockResolvedValue(1)
  mocks.sessionUpdate.mockReset().mockResolvedValue(1)
  mocks.streamChat.mockReset()
  mocks.records.length = 0
  // 默认按「浏览器没有 Web Locks」跑：这些用例都是单标签页场景，等价于改动前的行为。
  // 真实锁管理器还会在用例之间留下没释放的锁（挂起的 pendingStream 不会被中止），
  // 下一个用例的同名会话就直接拿不到锁了。
  vi.stubGlobal('navigator', {})
})

afterEach(() => {
  for (const sessionId of ['a', ...activeSessionIds()]) removeRun(sessionId)
  flushNow()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('flushAllRunsNow', () => {
  it('把进行中 run 的最新内容立刻落库（不等节流窗口）', async () => {
    pendingStream({ type: 'content', delta: 'A' }, { type: 'content', delta: 'B' })

    start()
    await vi.waitFor(() => expect(getRun('a')?.content).toBe('AB'))

    mocks.update.mockClear()
    flushAllRunsNow()

    expect(mocks.update).toHaveBeenCalledTimes(1)
    expect(mocks.update).toHaveBeenCalledWith(
      'm_a',
      expect.objectContaining({ content: 'AB', incomplete: true }),
    )
  })

  it('已结束的 run 不再补写（终态由 saveFinal 负责）', async () => {
    finishingStream({ type: 'content', delta: '答完' }, { type: 'done' })

    start()
    await vi.waitFor(() => expect(getRun('a')?.finished).toBe(true))

    mocks.update.mockClear()
    flushAllRunsNow()

    expect(mocks.update).not.toHaveBeenCalled()
  })
})

describe('关页/切后台补写', () => {
  it('pagehide 与 visibilitychange(hidden) 时立即落库', async () => {
    const listeners = new Map<string, () => void>()
    const fakeWindow = {
      addEventListener: vi.fn((type: string, listener: () => void) => listeners.set(type, listener)),
      removeEventListener: vi.fn(),
    }
    const fakeDocument = {
      addEventListener: vi.fn((type: string, listener: () => void) =>
        listeners.set(`doc:${type}`, listener),
      ),
      removeEventListener: vi.fn(),
      visibilityState: 'hidden',
    }
    vi.stubGlobal('window', fakeWindow)
    vi.stubGlobal('document', fakeDocument)

    const unbind = registerLifecycleFlush()
    try {
      pendingStream({ type: 'content', delta: '最后一句' })
      start()
      await vi.waitFor(() => expect(getRun('a')?.content).toBe('最后一句'))

      mocks.update.mockClear()
      listeners.get('pagehide')!()
      expect(mocks.update).toHaveBeenCalledWith(
        'm_a',
        expect.objectContaining({ content: '最后一句' }),
      )

      mocks.update.mockClear()
      listeners.get('doc:visibilitychange')!()
      expect(mocks.update).toHaveBeenCalledTimes(1)

      // 页面可见时不做无谓写入
      mocks.update.mockClear()
      fakeDocument.visibilityState = 'visible'
      listeners.get('doc:visibilitychange')!()
      expect(mocks.update).not.toHaveBeenCalled()
    } finally {
      unbind()
    }
  })

  it('node 环境（没有 window/document）注册是空操作', () => {
    // 模块加载时已经调用过一次；这里只是确认它不会抛，也不留监听
    expect(typeof window).toBe('undefined')
    expect(() => registerLifecycleFlush()).not.toThrow()
  })
})

describe('落库失败', () => {
  it('终态落库失败时保留内存态、只报错一次', async () => {
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    mocks.update.mockRejectedValue(new Error('QuotaExceededError'))

    finishingStream({ type: 'content', delta: '刚流出的回复' }, { type: 'done' })

    start()
    await vi.waitFor(() => expect(getRun('a')?.finished).toBe(true))

    expect(getRun('a')!.persistFailed).toBe(true)
    expect(errorSpy).toHaveBeenCalledTimes(1)
    expect(String(errorSpy.mock.calls[0][0])).toContain('落库失败')

    // ChatProvider 读回的旧 DB 内容不能盖掉内存态：clearFinishedRun 必须放行不了
    clearFinishedRun('a')
    expect(getRun('a')!.content).toBe('刚流出的回复')
  })

  it('落库成功时结束的 run 照旧可以被丢弃', async () => {
    finishingStream({ type: 'content', delta: '答完' }, { type: 'done' })

    start()
    await vi.waitFor(() => expect(getRun('a')?.finished).toBe(true))

    clearFinishedRun('a')

    expect(getRun('a')).toBeNull()
  })
})

describe('中止', () => {
  it('用户中止时 stopped 一并落库', async () => {
    mocks.streamChat.mockImplementation(async function* (params: { signal: AbortSignal }) {
      await new Promise((_resolve, reject) => {
        const fail = (): void =>
          reject(Object.assign(new Error('已中止'), { name: 'AbortError' }))
        if (params.signal.aborted) fail()
        else params.signal.addEventListener('abort', fail, { once: true })
      })
      yield { type: 'done' } as const
    })

    start()
    await vi.waitFor(() => expect(mocks.streamChat).toHaveBeenCalled())

    abortRun('a')
    await vi.waitFor(() => expect(getRun('a')?.finished).toBe(true))

    expect(getRun('a')!.stopped).toBe(true)
    expect(getRun('a')!.incomplete).toBe(false)
    expect(mocks.update.mock.calls.at(-1)?.[1]).toEqual(
      expect.objectContaining({ stopped: true, incomplete: false }),
    )
  })
})

/**
 * 可控的锁管理器：语义与 Web Locks 的 `ifAvailable` 一致——拿不到锁时回调照旧被调用、
 * 参数为 null；拿到则持锁到回调返回的 promise 结算。
 */
function fakeLocks(heldNames: string[] = []) {
  const held = new Set(heldNames)
  const request = vi.fn(
    async (name: string, _options: unknown, callback: (lock: unknown) => unknown) => {
      if (held.has(name)) return callback(null)

      held.add(name)
      try {
        return await callback({ name })
      } finally {
        held.delete(name)
      }
    },
  )
  return { held, request }
}

describe('跨标签互斥', () => {
  beforeEach(() => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
  })

  it('锁被另一个标签页占着时：立即以错误收尾，不发请求，并把提示落库', async () => {
    const locks = fakeLocks(['bennett:chat-run:a'])
    vi.stubGlobal('navigator', { locks })

    start()
    await vi.waitFor(() => expect(getRun('a')?.finished).toBe(true))

    expect(mocks.streamChat).not.toHaveBeenCalled()
    // 提示必须落库：只写内存的话，finished-run 回读后会留下「无解释的空白占位」。
    // 不写流式进度、也不 bump 会话时间戳（内容归持锁那一页）。
    expect(mocks.update).toHaveBeenCalledTimes(1)
    const payload = mocks.update.mock.calls[0][1] as { content: string; stopped: boolean }
    expect(payload.content).toContain('另一个标签页正在生成回复，本次未发送')
    expect(payload.stopped).toBe(true)
    expect(mocks.sessionUpdate).not.toHaveBeenCalled()

    const run = getRun('a')!
    expect(run.content).toContain('另一个标签页正在生成回复，本次未发送')
    expect(run.stopped).toBe(true)
    expect(run.incomplete).toBe(false)
  })

  it('拿到锁时照常生成，并在 run 结束后释放锁', async () => {
    const locks = fakeLocks()
    vi.stubGlobal('navigator', { locks })
    finishingStream({ type: 'content', delta: '答完' }, { type: 'done' })

    start()
    await vi.waitFor(() => expect(getRun('a')?.finished).toBe(true))

    expect(locks.request).toHaveBeenCalledWith(
      'bennett:chat-run:a',
      { ifAvailable: true },
      expect.any(Function),
    )
    expect(getRun('a')!.content).toBe('答完')
    expect(mocks.update).toHaveBeenCalled()
    // 终态落库之后才释放：锁不会漏在一个已经结束的 run 手里
    await vi.waitFor(() => expect(locks.held.size).toBe(0))
  })

  it('锁不可用（旧浏览器）时不互斥，行为与改动前一致', async () => {
    vi.stubGlobal('navigator', {})
    finishingStream({ type: 'content', delta: '答完' }, { type: 'done' })

    start()
    await vi.waitFor(() => expect(getRun('a')?.finished).toBe(true))

    expect(mocks.streamChat).toHaveBeenCalledTimes(1)
    expect(getRun('a')!.content).toBe('答完')
  })
})

describe('跨标签失效后的消息重读', () => {
  const record = (
    overrides: Partial<MessageRecord> & Pick<MessageRecord, 'id' | 'role'>,
  ): MessageRecord => ({ sessionId: 'a', content: '', createdAt: 1, ...overrides })

  it('造一个终态 run 让 UI 回读，内容与库里一致，且不落库', async () => {
    mocks.records.push(
      record({
        id: 'm_a',
        role: 'assistant',
        content: '另一个标签页写的内容',
        thinking: '想过了',
        thinkingMs: 12,
        partOrder: ['reasoning', 'text'],
        incomplete: true,
      }),
    )

    await requestSessionMessagesReload('a')

    const run = getRun('a')!
    // ChatProvider 只认「本会话有一个已结束的 run」，据此回读整表并丢弃内存态
    expect(run.finished).toBe(true)
    expect(run.assistantMsgId).toBe('m_a')
    // 覆盖层按库里那条记录摆好，回读落地前不会闪出空白气泡
    expect(run.content).toBe('另一个标签页写的内容')
    expect(run.thinking).toBe('想过了')
    expect(run.thinkingMs).toBe(12)
    expect(run.partOrder).toEqual(['reasoning', 'text'])
    // 不完整标记按库里的取值走，不能因为「没收全」被算成半截
    expect(run.incomplete).toBe(true)
    expect(run.stopped).toBe(false)
    // 这个 run 只活在内存里
    expect(mocks.update).not.toHaveBeenCalled()
    expect(mocks.sessionUpdate).not.toHaveBeenCalled()
  })

  it('库里还没有助手消息时不造 run', async () => {
    mocks.records.push(record({ id: 'u1', role: 'user', content: '提问' }))

    await requestSessionMessagesReload('a')

    expect(getRun('a')).toBeNull()
  })

  it('本页已有 run 内存态时跳过，不顶掉内存里的内容', async () => {
    mocks.records.push(record({ id: 'm_a', role: 'assistant', content: '库里的旧内容' }))
    pendingStream({ type: 'content', delta: '本页刚流出的' })

    start()
    await vi.waitFor(() => expect(getRun('a')?.content).toBe('本页刚流出的'))

    await requestSessionMessagesReload('a')

    expect(getRun('a')!.content).toBe('本页刚流出的')
    expect(getRun('a')!.finished).toBe(false)
  })
})
