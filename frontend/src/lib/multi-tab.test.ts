import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { TAB_ID, acquireRunLock, notifySessionsChanged, subscribeSessionsChanged } from './multi-tab'
import type { SessionsChangedMessage } from './multi-tab'

interface FakeEvent {
  data: unknown
}

/**
 * 假 BroadcastChannel：按频道名把消息投给所有实例，**包括发送方自己**。
 *
 * 真实实现不会回传给发送方，但「自己发的消息必须忽略」是订阅端的契约（TAB_ID 过滤），
 * 这里刻意不替它兜底——否则那条过滤逻辑永远测不到。
 */
class FakeBroadcastChannel {
  static instances: FakeBroadcastChannel[] = []

  readonly name: string
  onmessage: ((event: FakeEvent) => void) | null = null
  received: unknown[] = []
  closed = false

  constructor(name: string) {
    this.name = name
    FakeBroadcastChannel.instances.push(this)
  }

  postMessage(data: unknown): void {
    for (const instance of FakeBroadcastChannel.instances) {
      if (instance.name !== this.name || instance.closed) continue
      instance.received.push(data)
      instance.onmessage?.({ data })
    }
  }

  close(): void {
    this.closed = true
  }
}

/** 订阅端通道，也就是挂上 onmessage 的那个实例。 */
function subscriberChannel(): FakeBroadcastChannel {
  const channel = FakeBroadcastChannel.instances.find((instance) => instance.onmessage !== null)
  if (!channel) throw new Error('没有找到订阅端通道')
  return channel
}

/** 模拟另一个标签页发通知：新建一个实例发（发完即弃，它不会收到后续消息）。 */
function postFromOtherTab(sessionId: string, sourceId = 'tab_other'): void {
  new FakeBroadcastChannel('bennett:sessions').postMessage({ sessionId, sourceId })
}

/**
 * 可控的锁管理器：语义与 Web Locks 的 `ifAvailable` 一致——拿不到锁时回调照旧被调用，
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

beforeEach(() => {
  FakeBroadcastChannel.instances = []
  vi.stubGlobal('BroadcastChannel', FakeBroadcastChannel)
  vi.spyOn(console, 'warn').mockImplementation(() => {})
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('跨标签失效通知', () => {
  it('其他标签页的通知会按频道名送达订阅者', () => {
    const seen: SessionsChangedMessage[] = []
    const unsubscribe = subscribeSessionsChanged((message) => seen.push(message))
    try {
      postFromOtherTab('s1')

      expect(seen).toEqual([{ sessionId: 's1', sourceId: 'tab_other' }])
    } finally {
      unsubscribe()
    }
  })

  it('自己发的通知被忽略（消息已送达，靠 sourceId 过滤）', () => {
    const seen: SessionsChangedMessage[] = []
    const unsubscribe = subscribeSessionsChanged((message) => seen.push(message))
    try {
      const receiver = subscriberChannel()
      notifySessionsChanged('s1')

      expect(receiver.received).toEqual([{ sessionId: 's1', sourceId: TAB_ID }])
      expect(seen).toEqual([])
    } finally {
      unsubscribe()
    }
  })

  it('unsubscribe 之后关闭通道，不再触发回调', () => {
    const seen: SessionsChangedMessage[] = []
    const unsubscribe = subscribeSessionsChanged((message) => seen.push(message))
    const channel = subscriberChannel()

    postFromOtherTab('s1')
    expect(seen).toHaveLength(1)

    unsubscribe()
    expect(channel.closed).toBe(true)

    postFromOtherTab('s1')
    expect(seen).toHaveLength(1)
  })

  it('形状不对或缺少来源的消息被丢弃', () => {
    const seen: SessionsChangedMessage[] = []
    const unsubscribe = subscribeSessionsChanged((message) => seen.push(message))
    try {
      const channel = new FakeBroadcastChannel('bennett:sessions')
      channel.postMessage('not-an-object')
      channel.postMessage({ hello: 'world' })
      channel.postMessage({ sessionId: '', sourceId: 'tab_other' })
      channel.postMessage({ sessionId: 's1' })

      expect(seen).toEqual([])
    } finally {
      unsubscribe()
    }
  })

  it('没有 BroadcastChannel 时是空操作，不抛异常', () => {
    vi.stubGlobal('BroadcastChannel', undefined)

    expect(() => notifySessionsChanged('s1')).not.toThrow()

    const unsubscribe = subscribeSessionsChanged(() => {
      throw new Error('不该被调用')
    })
    expect(() => unsubscribe()).not.toThrow()
  })
})

describe('跨标签生成锁', () => {
  it('拿到锁后返回 held，持锁期间同一会话拿不到，释放后才能再拿', async () => {
    const locks = fakeLocks()
    vi.stubGlobal('navigator', { locks })

    const acquired = await acquireRunLock('s1')
    expect(acquired.kind).toBe('held')
    expect(locks.request).toHaveBeenCalledWith(
      'bennett:chat-run:s1',
      { ifAvailable: true },
      expect.any(Function),
    )

    // 不排队等待：持锁期间再来一次立刻判 busy
    expect((await acquireRunLock('s1')).kind).toBe('busy')

    if (acquired.kind === 'held') acquired.release()
    await vi.waitFor(() => expect(locks.held.size).toBe(0))
    expect((await acquireRunLock('s1')).kind).toBe('held')
  })

  it('锁被其他标签页占用时返回 busy', async () => {
    const locks = fakeLocks(['bennett:chat-run:s1'])
    vi.stubGlobal('navigator', { locks })

    expect(await acquireRunLock('s1')).toEqual({ kind: 'busy' })
  })

  it('没有 navigator.locks 时返回 unsupported（退回无互斥）', async () => {
    vi.stubGlobal('navigator', {})

    expect(await acquireRunLock('s1')).toEqual({ kind: 'unsupported' })
  })

  it('锁管理器同步抛错时按 unsupported 处理，不把生成堵死', async () => {
    vi.stubGlobal('navigator', {
      locks: {
        request: () => {
          throw new Error('策略禁用')
        },
      },
    })

    expect(await acquireRunLock('s1')).toEqual({ kind: 'unsupported' })
  })

  it('锁管理器返回 rejected promise 时同样按 unsupported 处理', async () => {
    vi.stubGlobal('navigator', {
      locks: {
        request: async () => {
          throw new Error('锁管理器不可用')
        },
      },
    })

    expect(await acquireRunLock('s1')).toEqual({ kind: 'unsupported' })
  })
})
