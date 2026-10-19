import { afterEach, describe, expect, it, vi } from 'vitest'

import { TRANSPORT_INCOMPLETE, type ChatEvent } from '../types'
import { API_BASE_URL } from './constants'
import { IDLE_TIMEOUT_MS, streamChat } from './chat-transport'

const encoder = new TextEncoder()

/** 原始字节分片：跨 chunk 的多字节 UTF-8 序列只有这样才测得出来。 */
function bodyFromBytes(chunks: Uint8Array[], cancel?: () => void): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk)
      controller.close()
    },
    cancel,
  })
}

/** 按给定分片构造响应体，用来验证「网络分片边界」不影响分帧。 */
function bodyFrom(chunks: string[]): ReadableStream<Uint8Array> {
  return bodyFromBytes(chunks.map((chunk) => encoder.encode(chunk)))
}

/** 永不关闭的响应体：模拟「后端还在生成」与「半开连接（不再发数据也不关闭）」。 */
function openEndedBody(chunks: string[], cancel?: () => void): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
    },
    cancel,
  })
}

/**
 * 断言用：ChatEvent 是判别联合，字段只属于其中若干分支。用例只读 type/delta/damaged，
 * 这里放宽一次，免得每处断言都写 Extract<>。
 */
type AssertedEvent = ChatEvent & { delta?: string; damaged?: boolean }

/** 用例不关心取消：给一个永不 abort 的真实 signal（契约把它标成必填）。 */
const NO_SIGNAL = new AbortController().signal

interface StubFetchOptions {
  ok?: boolean
  status?: number
  body?: ReadableStream<Uint8Array> | null
  json?: () => Promise<unknown>
  headers?: Record<string, string>
}

/** 只声明用例读到的那些 init 字段；真实调用不由这个 mock 的签名做类型校验。 */
interface StubFetchInit {
  method: string
  body: string
  signal?: AbortSignal
}

function stubFetch({ ok = true, status = 200, body = null, json, headers = {} }: StubFetchOptions = {}) {
  const fetchMock = vi.fn(async (_url: string, _init: StubFetchInit) => ({
    ok,
    status,
    body,
    headers: { get: (name: string) => headers[name] ?? null },
    json: json ?? (async () => ({})),
  }))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/** sse-starlette 实际发出的分帧（CRLF，实测自 /api/chat）。 */
const frame = (payload: object) => `data: ${JSON.stringify(payload)}\r\n\r\n`

async function collectFrom(body: ReadableStream<Uint8Array>): Promise<AssertedEvent[]> {
  stubFetch({ body })
  const events: AssertedEvent[] = []
  for await (const event of streamChat({
    model: 'm',
    reasoningEffort: null,
    messages: [],
    signal: NO_SIGNAL,
  })) {
    events.push(event)
  }
  return events
}

const collect = (chunks: string[]): Promise<AssertedEvent[]> => collectFrom(bodyFrom(chunks))
const collectBytes = (chunks: Uint8Array[]): Promise<AssertedEvent[]> => collectFrom(bodyFromBytes(chunks))

/** 只关心「整条流跑完」的场景（错误响应、异常传播）。 */
async function drain(): Promise<void> {
  for await (const _ of streamChat({
    model: null,
    reasoningEffort: null,
    messages: [],
    signal: NO_SIGNAL,
  })) {
    // 消费掉
  }
}

/** 让挂起的微任务跑干净：fake timers 下没有宏任务可用，只能靠 await 让出队列。 */
async function settle(): Promise<void> {
  for (let i = 0; i < 25; i += 1) await Promise.resolve()
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('请求体', () => {
  it('发送 model / reasoning_effort / messages，且不再发送已废弃的 stream 字段', async () => {
    const fetchMock = stubFetch({ body: bodyFrom([frame({ type: 'done' })]) })

    for await (const _ of streamChat({
      model: 'openai/gpt-4o',
      reasoningEffort: 'high',
      messages: [{ role: 'user', content: '你好' }],
      signal: NO_SIGNAL,
    })) {
      // 消费掉
    }

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe(`${API_BASE_URL}/api/chat`)
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({
      model: 'openai/gpt-4o',
      reasoning_effort: 'high',
      messages: [{ role: 'user', content: '你好' }],
    })
  })

  it('有锚点时按 snake_case 带上 context_anchor', async () => {
    const fetchMock = stubFetch({ body: bodyFrom([frame({ type: 'done' })]) })

    for await (const _ of streamChat({
      model: 'openai/gpt-4o',
      reasoningEffort: null,
      messages: [
        { role: 'user', content: '问' },
        { role: 'assistant', content: '答' },
        { role: 'user', content: '再问' },
      ],
      contextAnchor: { messageIndex: 1, contextTokens: 1065 },
      signal: NO_SIGNAL,
    })) {
      // 消费掉
    }

    const [url, init] = fetchMock.mock.calls[0]
    expect(JSON.parse(init.body)).toEqual({
      model: 'openai/gpt-4o',
      reasoning_effort: null,
      messages: [
        { role: 'user', content: '问' },
        { role: 'assistant', content: '答' },
        { role: 'user', content: '再问' },
      ],
      context_anchor: { message_index: 1, context_tokens: 1065 },
    })
    void url
  })

  it('没有锚点时整个字段都不出现（老后端不认识它也能照常跑）', async () => {
    const fetchMock = stubFetch({ body: bodyFrom([frame({ type: 'done' })]) })

    for await (const _ of streamChat({
      model: null,
      reasoningEffort: null,
      messages: [{ role: 'user', content: '你好' }],
      signal: NO_SIGNAL,
    })) {
      // 消费掉
    }

    const [, init] = fetchMock.mock.calls[0]
    expect('context_anchor' in JSON.parse(init.body)).toBe(false)
  })

  it('把 AbortSignal 透传给 fetch', async () => {
    const controller = new AbortController()
    const fetchMock = stubFetch({ body: bodyFrom([frame({ type: 'done' })]) })

    for await (const _ of streamChat({
      model: null,
      reasoningEffort: null,
      messages: [],
      signal: controller.signal,
    })) {
      // 消费掉
    }

    expect(fetchMock.mock.calls[0][1].signal).toBe(controller.signal)
  })
})

describe('分帧解析', () => {
  it('逐个产出事件', async () => {
    const events = await collect([
      frame({ type: 'thinking', delta: '嗯' }),
      frame({ type: 'content', delta: '你好' }),
      frame({ type: 'done' }),
    ])

    expect(events.map((e) => e.type)).toEqual(['thinking', 'content', 'done'])
    expect(events[1].delta).toBe('你好')
  })

  it('真实字节级切分：在每个字节位置断开都不影响分帧', async () => {
    // 用码点切分（[...whole]）每片都是完整汉字，测不出「多字节序列跨 chunk」——
    // 那才是 TextDecoder 需要 stream:true 的原因。这里按字节切。
    const bytes = encoder.encode(frame({ type: 'content', delta: '你好' }) + frame({ type: 'done' }))

    for (let split = 1; split < bytes.length; split += 1) {
      const events = await collectBytes([bytes.slice(0, split), bytes.slice(split)])

      expect(events.map((e) => e.type)).toEqual(['content', 'done'])
      expect(events[0].delta).toBe('你好')
    }
  })

  it('接受 LF 结尾的帧（非所有服务端都用 CRLF）', async () => {
    const events = await collect(['data: {"type":"done"}\n\n'])

    expect(events.map((e) => e.type)).toEqual(['done'])
  })

  it('忽略心跳注释与空帧', async () => {
    const events = await collect([': ping\r\n\r\n', '\r\n', frame({ type: 'done' })])

    expect(events.map((e) => e.type)).toEqual(['done'])
  })

  it('多行 data 用换行拼接后再解析', async () => {
    // 服务端把 JSON 拆到多行 data 上（SSE 规范允许）
    const payload = '{"type":"content",\n"delta":"x"}'
    const [first, second] = payload.split('\n')
    const events = await collect([`data: ${first}\ndata: ${second}\r\n\r\n`, frame({ type: 'done' })])

    expect(events[0]).toEqual({ type: 'content', delta: 'x' })
  })

  it('流结束时缓冲区里的最后一帧不会丢（服务端没以空行收尾）', async () => {
    const events = await collect(['data: {"type":"done"}'])

    expect(events.map((e) => e.type)).toEqual(['done'])
  })

  it('正文里的换行不会撕裂分帧（JSON 已转义）', async () => {
    const events = await collect([frame({ type: 'content', delta: '第一行\n第二行' }), frame({ type: 'done' })])

    expect(events[0].delta).toBe('第一行\n第二行')
    expect(events).toHaveLength(2)
  })
})

describe('损坏与中断', () => {
  it('无法解析的帧被跳过，且标记为 damaged', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      const events = await collect(['data: {不是 JSON}\r\n\r\n', frame({ type: 'content', delta: 'ok' })])
      const incomplete = events.at(-1)!

      expect(events[0]).toEqual({ type: 'content', delta: 'ok' })
      expect(incomplete.type).toBe(TRANSPORT_INCOMPLETE)
      expect(incomplete.damaged).toBe(true)
      expect(consoleError).toHaveBeenCalled()
    } finally {
      consoleError.mockRestore()
    }
  })

  it.each(['null', '123', '{}'])('形状不对的载荷 %s 走解析错误路径，而不是把 JS 报错抛给用户', async (payload) => {
    // 旧实现只做 `JSON.parse(...) as ChatEvent`：`data: null` 被原样产出，紧接着
    // `event.type` 抛 TypeError，整条流断在这里，用户看到的是原始 JS 报错。
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      const events = await collect([`data: ${payload}\r\n\r\n`, frame({ type: 'content', delta: 'ok' })])

      expect(events.map((e) => e.type)).toEqual(['content', TRANSPORT_INCOMPLETE])
      expect(events.at(-1)?.damaged).toBe(true)
      expect(consoleError).toHaveBeenCalled()
    } finally {
      consoleError.mockRestore()
    }
  })

  it('合法事件不受形状校验影响（未知类型留给 store 忽略）', async () => {
    const events = await collect([
      frame({ type: 'thinking', delta: '嗯' }),
      frame({ type: 'brand_new_event', anything: 1 }),
      frame({ type: 'done' }),
    ])

    expect(events.map((e) => e.type)).toEqual(['thinking', 'brand_new_event', 'done'])
  })

  it('流截断在汉字中间：残留的半个多字节序列不会被静默吞掉', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      // 真实字节切分：切在后一个「好」的 3 字节中间（其后还有 `"}` + CRLFCRLF 共 6 字节）
      const bytes = encoder.encode(frame({ type: 'content', delta: '你好' }))
      const cut = bytes.length - 7

      const events = await collectBytes([bytes.slice(0, cut)])

      expect(events.map((e) => e.type)).toEqual([TRANSPORT_INCOMPLETE])
      expect(events.at(-1)?.damaged).toBe(true)
      // flush 之前这半个序列被 decoder 丢在原地，诊断里连替换字符都看不到
      const payload = consoleError.mock.calls.at(-1)?.[3] as string
      expect(payload.endsWith('\uFFFD')).toBe(true)
    } finally {
      consoleError.mockRestore()
    }
  })

  it('缺少 done 时合成 INCOMPLETE 事件', async () => {
    const events = await collect([frame({ type: 'content', delta: '半截' })])

    expect(events.map((e) => e.type)).toEqual(['content', TRANSPORT_INCOMPLETE])
    expect(events.at(-1)!.damaged).toBe(false)
  })

  it('收到 done 就不再补 INCOMPLETE', async () => {
    const events = await collect([frame({ type: 'content', delta: '完整' }), frame({ type: 'done' })])

    expect(events.map((e) => e.type)).toEqual(['content', 'done'])
  })

  it('error 也是合法终止事件，不该再补 INCOMPLETE', async () => {
    // 服务端契约是「以 done **或 error** 收尾」。只认 done 会把正常的错误路径
    // 误判成「连接中断」——用户先看到错误、又看到一句「连接已中断」，既错又重复。
    // 这是用真实服务端字节验证时发现的缺陷。
    const events = await collect([frame({ type: 'error', message: '生成异常，请重试（错误编号 abc123）' })])

    expect(events.map((e) => e.type)).toEqual(['error'])
  })

  it('丢过帧但最后收到了 done，仍然要告知内容有缺损', async () => {
    // 否则用户会在不知情的情况下读到残缺回复：done 只说明流干净结束了，
    // 不能抹掉中途已经丢掉的那一帧。
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      const events = await collect([
        'data: {不是 JSON}\r\n\r\n',
        frame({ type: 'content', delta: 'ok' }),
        frame({ type: 'done' }),
      ])

      expect(events.map((e) => e.type)).toEqual(['content', 'done', TRANSPORT_INCOMPLETE])
      expect(events.at(-1)?.damaged).toBe(true)
    } finally {
      consoleError.mockRestore()
    }
  })

  it('正常流读取结束后释放 reader 锁', async () => {
    const stream = bodyFrom([frame({ type: 'content', delta: 'ok' }), frame({ type: 'done' })])
    stubFetch({ body: stream })

    for await (const _ of streamChat({ model: null, reasoningEffort: null, messages: [], signal: NO_SIGNAL })) {
      // 消费掉
    }

    // 若 releaseLock 没有被调用，locked 会为 true，并且再次 getReader 会抛出 TypeError
    expect(stream.locked).toBe(false)
  })

  it('流解析发生中断/异常时仍 cancel 底层流并释放 reader 锁', async () => {
    const cancel = vi.fn()
    const stream = openEndedBody([frame({ type: 'content', delta: 'ok' })], cancel)
    stubFetch({ body: stream })

    await expect(
      (async () => {
        for await (const _ of streamChat({ model: null, reasoningEffort: null, messages: [], signal: NO_SIGNAL })) {
          throw new Error('中途打断')
        }
      })(),
    ).rejects.toThrow('中途打断')

    // 只 releaseLock 不会关闭流：后端会继续生成，连接也不会释放
    expect(cancel).toHaveBeenCalledTimes(1)
    expect(stream.locked).toBe(false)
  })

  it('提前 break 时 cancel 底层流，不让后端继续消耗额度', async () => {
    const cancel = vi.fn()
    stubFetch({ body: openEndedBody([frame({ type: 'content', delta: '边流边看' })], cancel) })

    for await (const _ of streamChat({ model: null, reasoningEffort: null, messages: [], signal: NO_SIGNAL })) {
      break
    }

    expect(cancel).toHaveBeenCalledTimes(1)
  })

  it('用户中止的 AbortError 原样上抛：清理期的 cancel 报错不覆盖它', async () => {
    // 真实场景：signal abort 后响应体已 errored，此时再 cancel 会以同一个错误 reject
    // （规范如此）。清理动作必须吞掉它——chat-runs 靠 err.name === 'AbortError' 分辨
    // 「用户点了停止」和「请求失败」，被覆盖成别的错误就会把停止显示成失败。
    const abortError = new DOMException('The operation was aborted.', 'AbortError')
    let reads = 0
    stubFetch({
      body: new ReadableStream<Uint8Array>({
        pull(controller) {
          reads += 1
          if (reads === 1) controller.enqueue(encoder.encode(frame({ type: 'content', delta: '半截' })))
          else controller.error(abortError)
        },
        cancel: () => Promise.reject(abortError),
      }),
    })

    await expect(drain()).rejects.toMatchObject({ name: 'AbortError' })
  })

  it('空闲超过阈值没有任何字节：中止请求并按 INCOMPLETE 收尾', async () => {
    vi.useFakeTimers()
    try {
      const cancel = vi.fn()
      stubFetch({ body: openEndedBody([frame({ type: 'thinking', delta: '想' })], cancel) })

      const events: AssertedEvent[] = []
      const pump = (async () => {
        for await (const event of streamChat({ model: null, reasoningEffort: null, messages: [], signal: NO_SIGNAL })) {
          events.push(event)
        }
      })()

      // 第一个分片到手，之后连接半开：下一次 read 永远不返回
      await settle()
      expect(events.map((e) => e.type)).toEqual(['thinking'])
      expect(cancel).not.toHaveBeenCalled()

      await vi.advanceTimersByTimeAsync(IDLE_TIMEOUT_MS + 1)
      await pump

      expect(cancel).toHaveBeenCalledTimes(1)
      expect(events.map((e) => e.type)).toEqual(['thinking', TRANSPORT_INCOMPLETE])
      expect(events.at(-1)?.damaged).toBe(false)
    } finally {
      vi.useRealTimers()
    }
  })

  it('持续有数据的长回答不会被空闲看门狗误杀', async () => {
    vi.useFakeTimers()
    try {
      const cancel = vi.fn()
      let source!: ReadableStreamDefaultController<Uint8Array>
      stubFetch({
        body: new ReadableStream<Uint8Array>({
          start(controller) {
            source = controller
          },
          cancel,
        }),
      })
      const push = (payload: object): void => source.enqueue(encoder.encode(frame(payload)))

      const events: AssertedEvent[] = []
      const pump = (async () => {
        for await (const event of streamChat({ model: null, reasoningEffort: null, messages: [], signal: NO_SIGNAL })) {
          events.push(event)
        }
      })()

      push({ type: 'content', delta: '0' })
      await settle()

      // 每 59s 来一个分片，总时长远超阈值：只要一直有字节就不该被杀
      for (let i = 1; i <= 5; i += 1) {
        await vi.advanceTimersByTimeAsync(IDLE_TIMEOUT_MS - 1_000)
        push({ type: 'content', delta: String(i) })
        await settle()
      }

      push({ type: 'done' })
      source.close()
      await pump

      expect(events.map((e) => e.type)).toEqual([
        'content',
        'content',
        'content',
        'content',
        'content',
        'content',
        'done',
      ])
      expect(cancel).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('响应没有可读流时报错而不是静默产出空流', async () => {
    stubFetch({ body: null })

    await expect(
      (async () => {
        for await (const _ of streamChat({ model: null, reasoningEffort: null, messages: [], signal: NO_SIGNAL })) {
          // 消费掉
        }
      })(),
    ).rejects.toThrow('响应没有可读的流式主体')
  })
})

describe('错误响应', () => {
  const errorResponse = (status: number, json: () => Promise<unknown>, headers?: Record<string, string>) =>
    stubFetch({ ok: false, status, json, headers })

  it('429 带上 Retry-After 秒数，告诉用户要等多久', async () => {
    stubFetch({ ok: false, status: 429, headers: { 'Retry-After': '17' } })

    await expect(drain()).rejects.toThrow('HTTP 429: 请求过于频繁，请 17 秒后重试')
  })

  it('429 没有 Retry-After 时退回「请稍后重试」', async () => {
    stubFetch({ ok: false, status: 429 })

    await expect(drain()).rejects.toThrow('HTTP 429: 请求过于频繁，请稍后重试')
  })

  it('429 的 Retry-After 不是秒数（反代塞了别的值）时同样退回「请稍后重试」', async () => {
    stubFetch({ ok: false, status: 429, headers: { 'Retry-After': 'Wed, 21 Oct 2026 07:28:00 GMT' } })

    await expect(drain()).rejects.toThrow('HTTP 429: 请求过于频繁，请稍后重试')
  })

  it('422 把具体超限字段带进错误信息', async () => {
    errorResponse(422, async () => ({
      detail: [
        {
          type: 'string_too_long',
          loc: ['body', 'messages', 0, 'content'],
          msg: 'String should have at most 32000 characters',
        },
      ],
    }))

    await expect(
      (async () => {
        for await (const _ of streamChat({ model: null, reasoningEffort: null, messages: [], signal: NO_SIGNAL })) {
          // 消费掉
        }
      })(),
    ).rejects.toThrow('HTTP 422: messages.0.content: String should have at most 32000 characters')
  })

  it('总预算类错误（loc 只有 body）只展示 msg', async () => {
    errorResponse(422, async () => ({
      detail: [{ type: 'value_error', loc: ['body'], msg: '对话历史总长度 416000 字符超出上限 400000 字符' }],
    }))

    await expect(
      (async () => {
        for await (const _ of streamChat({ model: null, reasoningEffort: null, messages: [], signal: NO_SIGNAL })) {
          // 消费掉
        }
      })(),
    ).rejects.toThrow('HTTP 422: 对话历史总长度 416000 字符超出上限 400000 字符')
  })

  it('detail 是字符串时直接用', async () => {
    errorResponse(502, async () => ({ detail: '上游不可用' }))

    await expect(
      (async () => {
        for await (const _ of streamChat({ model: null, reasoningEffort: null, messages: [], signal: NO_SIGNAL })) {
          // 消费掉
        }
      })(),
    ).rejects.toThrow('HTTP 502: 上游不可用')
  })

  it('响应体不是 JSON（例如反代返回 HTML）时退回状态码', async () => {
    errorResponse(504, async () => {
      throw new SyntaxError('Unexpected token <')
    })

    await expect(
      (async () => {
        for await (const _ of streamChat({ model: null, reasoningEffort: null, messages: [], signal: NO_SIGNAL })) {
          // 消费掉
        }
      })(),
    ).rejects.toThrow('HTTP 错误: 504')
  })
})
