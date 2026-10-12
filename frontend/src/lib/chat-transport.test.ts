import { afterEach, describe, expect, it, vi } from 'vitest'

import { TRANSPORT_INCOMPLETE, type ChatEvent } from '../types'
import { API_BASE_URL } from './constants'
import { streamChat } from './chat-transport'

const encoder = new TextEncoder()

/** 按给定分片构造响应体，用来验证「网络分片边界」不影响分帧。 */
function bodyFrom(chunks: string[]): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
      controller.close()
    },
  })
}

/**
 * 断言用：ChatEvent 是判别联合，字段只属于其中若干分支。用例只读 type/delta/damaged，
 * 这里放宽一次，免得每处断言都写 Extract<>。
 */
type AssertedEvent = ChatEvent & { delta?: string; damaged?: boolean }

/** 用例不关心取消，显式传 undefined 模拟「未提供 signal」；streamChat 的契约把它标成必填。 */
const NO_SIGNAL = undefined as unknown as AbortSignal

interface StubFetchOptions {
  ok?: boolean
  status?: number
  body?: ReadableStream<Uint8Array> | null
  json?: () => Promise<unknown>
}

/** 只声明用例读到的那些 init 字段；真实调用不由这个 mock 的签名做类型校验。 */
interface StubFetchInit {
  method: string
  body: string
  signal?: AbortSignal
}

function stubFetch({ ok = true, status = 200, body = null, json }: StubFetchOptions = {}) {
  const fetchMock = vi.fn(async (_url: string, _init: StubFetchInit) => ({
    ok,
    status,
    body,
    json: json ?? (async () => ({})),
  }))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

/** sse-starlette 实际发出的分帧（CRLF，实测自 /api/chat）。 */
const frame = (payload: object) => `data: ${JSON.stringify(payload)}\r\n\r\n`

async function collect(chunks: string[]): Promise<AssertedEvent[]> {
  stubFetch({ body: bodyFrom(chunks) })
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

  it('网络分片边界落在帧中间也不影响', async () => {
    const whole = frame({ type: 'content', delta: '你好' }) + frame({ type: 'done' })
    // 逐字节切开：最坏情况下的分片
    const chunks = [...whole].map((ch) => ch)

    const events = await collect(chunks)

    expect(events.map((e) => e.type)).toEqual(['content', 'done'])
    expect(events[0].delta).toBe('你好')
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

  it('流解析发生中断/异常时仍释放 reader 锁', async () => {
    const stream = bodyFrom([frame({ type: 'content', delta: 'ok' }), frame({ type: 'done' })])
    stubFetch({ body: stream })

    await expect(
      (async () => {
        for await (const _ of streamChat({ model: null, reasoningEffort: null, messages: [], signal: NO_SIGNAL })) {
          throw new Error('中途打断')
        }
      })(),
    ).rejects.toThrow('中途打断')

    expect(stream.locked).toBe(false)
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
  const errorResponse = (status: number, json: () => Promise<unknown>) => stubFetch({ ok: false, status, json })

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
