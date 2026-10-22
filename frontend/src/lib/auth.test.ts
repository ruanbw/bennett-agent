import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 登出编排会碰本地库与在途生成：db 要 IndexedDB、chat-runs 要真实 run 状态，两者在
 * node 环境都没有。这里只关心「登出无论如何都会去清理」，所以整模块替换掉。
 */
const mocks = vi.hoisted(() => ({
  clearAllLocalData: vi.fn<() => Promise<void>>(async () => {}),
  abortAllRuns: vi.fn<() => void>(() => {}),
}))

vi.mock('../db', () => ({ clearAllLocalData: mocks.clearAllLocalData }))
vi.mock('./chat-runs', () => ({ abortAllRuns: mocks.abortAllRuns }))

import { API_BASE_URL } from './constants'
import {
  addUnauthorizedListener,
  apiFetch,
  bumpAuthEpoch,
  getAuthEpoch,
  readErrorDetail,
  removeUnauthorizedListener,
  type UnauthorizedListener,
} from './api'
import {
  describeAuthError,
  describeConnectionError,
  fetchCurrentUser,
  login,
  loginWithSession,
  logout,
  logoutAndClearLocalData,
  modeAfterRegister,
  register,
  registerWithSession,
  withPending,
} from './auth'

interface StubResponse {
  ok?: boolean
  status?: number
  json?: () => Promise<unknown>
  /** 直接让 fetch 抛错，模拟网络故障 / 超时。 */
  error?: Error
}

/**
 * 记录每次调用的 (url, init)，并按预设序列返回响应（序列用完后重复最后一项）。
 *
 * 比事后读 `fetchMock.mock.calls` 好：那种写法在 `vi.fn` 的实现签名上很容易撞到
 * 类型推断的坑，而这里断言请求形状与「调用了几次」只需要这两个字段。
 */
function stubFetch(...responses: StubResponse[]) {
  const calls: Array<{ url: string; init: RequestInit }> = []
  let index = 0
  const fetchMock = vi.fn(async (url: string, init: RequestInit) => {
    const preset = responses[Math.min(index, responses.length - 1)] ?? {}
    index += 1
    calls.push({ url, init })
    if (preset.error) throw preset.error
    return {
      ok: preset.ok ?? true,
      status: preset.status ?? 200,
      json: preset.json ?? (async () => ({})),
    }
  })
  vi.stubGlobal('fetch', fetchMock)
  return { fetchMock, calls }
}

const asResponse = (json: () => Promise<unknown>) => ({ json }) as unknown as Response

/**
 * 监听器集合是模块级状态，用例之间必须隔离：注册过的都记账，结束统一注销。
 * （这也是 remove 语义的顺带验证：注销一个监听者不能影响别人。）
 */
const registeredListeners: UnauthorizedListener[] = []

function trackListener(listener: UnauthorizedListener): UnauthorizedListener {
  registeredListeners.push(listener)
  addUnauthorizedListener(listener)
  return listener
}

beforeEach(() => {
  mocks.clearAllLocalData.mockReset().mockResolvedValue(undefined)
  mocks.abortAllRuns.mockReset().mockReturnValue(undefined)
  // 失败路径本来就要打日志，用例里不希望刷屏；需要断言日志的用例直接看这两个 spy
  vi.spyOn(console, 'warn').mockImplementation(() => {})
  vi.spyOn(console, 'error').mockImplementation(() => {})
})

afterEach(() => {
  for (const listener of registeredListeners.splice(0)) removeUnauthorizedListener(listener)
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

// ---------------------------------------------------------------------
// apiFetch：全局 401 处理
// ---------------------------------------------------------------------
describe('apiFetch', () => {
  it('401 触发全局登出回调', async () => {
    stubFetch({ ok: false, status: 401 })
    const onUnauthorized = vi.fn()
    trackListener(onUnauthorized)

    await apiFetch('/api/models')

    expect(onUnauthorized).toHaveBeenCalledOnce()
  })

  it('notifyUnauthorized=false 时不触发（401 本身就是预期结果）', async () => {
    stubFetch({ ok: false, status: 401 })
    const onUnauthorized = vi.fn()
    trackListener(onUnauthorized)

    await apiFetch('/api/auth/me', { notifyUnauthorized: false })

    expect(onUnauthorized).not.toHaveBeenCalled()
  })

  it('非 401 不触发回调', async () => {
    stubFetch({ ok: false, status: 500 })
    const onUnauthorized = vi.fn()
    trackListener(onUnauthorized)

    await apiFetch('/api/models')

    expect(onUnauthorized).not.toHaveBeenCalled()
  })

  it('拼上基址并携带 cookie', async () => {
    const { calls } = stubFetch()

    await apiFetch('/api/models')

    expect(calls[0].url).toBe(`${API_BASE_URL}/api/models`)
    expect(calls[0].init.credentials).toBe('include')
  })

  it('透传调用方给的 init（method / body / signal）', async () => {
    const { calls } = stubFetch()
    const controller = new AbortController()

    await apiFetch('/api/chat', { method: 'POST', body: 'x', signal: controller.signal })

    expect(calls[0].init.method).toBe('POST')
    expect(calls[0].init.body).toBe('x')
    expect(calls[0].init.signal).toBe(controller.signal)
  })

  it('timeoutMs 到点中止请求（不再允许无限挂起）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(
        (_url: string, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            const fail = (): void => reject(Object.assign(new Error('已中止'), { name: 'AbortError' }))
            if (init.signal?.aborted) fail()
            else init.signal?.addEventListener('abort', fail, { once: true })
          })
      )
    )

    await expect(apiFetch('/api/auth/me', { timeoutMs: 5 })).rejects.toThrow()
  })
})

// ---------------------------------------------------------------------
// 401 监听者集合
// ---------------------------------------------------------------------
describe('401 监听者集合', () => {
  it('两个监听者都收到通知（单槽时代后来者会覆盖先注册的那个）', async () => {
    stubFetch({ ok: false, status: 401 })
    const first = vi.fn()
    const second = vi.fn()
    trackListener(first)
    trackListener(second)

    await apiFetch('/api/models')

    expect(first).toHaveBeenCalledOnce()
    expect(second).toHaveBeenCalledOnce()
  })

  it('注销一个监听者不影响另一个', async () => {
    stubFetch({ ok: false, status: 401 })
    const removed = vi.fn()
    const kept = vi.fn()
    trackListener(removed)
    trackListener(kept)

    removeUnauthorizedListener(removed)
    await apiFetch('/api/models')

    expect(removed).not.toHaveBeenCalled()
    expect(kept).toHaveBeenCalledOnce()
  })
})

// ---------------------------------------------------------------------
// 401 的世代（epoch）守卫
// ---------------------------------------------------------------------
describe('401 的世代（epoch）守卫', () => {
  it('登录前发出的请求拿到 401 时不触发登出（不能打掉刚建立的登录态）', async () => {
    let respond!: (response: unknown) => void
    const pending = new Promise((resolve) => {
      respond = resolve
    })
    vi.stubGlobal('fetch', vi.fn(async () => pending))

    const onUnauthorized = vi.fn()
    trackListener(onUnauthorized)

    const request = apiFetch('/api/models')
    // 期间用户登录成功：这条在途请求属于旧世代
    bumpAuthEpoch()
    respond({ ok: false, status: 401 })
    await request

    expect(onUnauthorized).not.toHaveBeenCalled()
  })

  it('当前世代的 401 照常触发登出', async () => {
    stubFetch({ ok: false, status: 401 })
    const onUnauthorized = vi.fn()
    trackListener(onUnauthorized)

    await apiFetch('/api/models')

    expect(onUnauthorized).toHaveBeenCalledWith(getAuthEpoch())
  })
})

// ---------------------------------------------------------------------
// readErrorDetail：三种 detail 形状
// ---------------------------------------------------------------------
describe('readErrorDetail', () => {
  it('对象 detail 优先取 reason', async () => {
    expect(await readErrorDetail(asResponse(async () => ({ detail: { code: 'X', reason: '说明' } })))).toBe('说明')
  })

  it('对象 detail 只有 code 时退回 code', async () => {
    expect(await readErrorDetail(asResponse(async () => ({ detail: { code: 'X' } })))).toBe('X')
  })
})

// ---------------------------------------------------------------------
// login：表单编码是最容易写错的约定
// ---------------------------------------------------------------------
describe('login', () => {
  it('用表单编码，字段名是 username 而不是 email', async () => {
    const { calls } = stubFetch({ ok: true, status: 204 })

    await login('a@example.com', 'hunter2hunter2')

    expect(calls[0].url).toBe(`${API_BASE_URL}/api/auth/login`)
    expect(calls[0].init.method).toBe('POST')
    expect(calls[0].init.headers).toMatchObject({
      'Content-Type': 'application/x-www-form-urlencoded',
    })

    const body = calls[0].init.body as URLSearchParams
    expect(body.get('username')).toBe('a@example.com')
    expect(body.get('password')).toBe('hunter2hunter2')
    // 后端读的是 username；发 email 会被当成缺少 username 而 422
    expect(body.get('email')).toBeNull()
  })

  it('成功返回 ok', async () => {
    stubFetch({ ok: true, status: 204 })

    await expect(login('a@example.com', 'hunter2hunter2')).resolves.toEqual({ ok: true })
  })

  it('凭据错误把错误码翻译成中文', async () => {
    stubFetch({ ok: false, status: 400, json: async () => ({ detail: 'LOGIN_BAD_CREDENTIALS' }) })

    await expect(login('a@example.com', 'wrong')).resolves.toEqual({
      ok: false,
      message: '邮箱或密码错误',
    })
  })

  it('登录失败不触发全局登出回调', async () => {
    stubFetch({ ok: false, status: 400, json: async () => ({ detail: 'LOGIN_BAD_CREDENTIALS' }) })
    const onUnauthorized = vi.fn()
    trackListener(onUnauthorized)

    await login('a@example.com', 'wrong')

    expect(onUnauthorized).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------
// register
// ---------------------------------------------------------------------
describe('register', () => {
  it('发 JSON', async () => {
    const { calls } = stubFetch({ ok: true, status: 201 })

    await register('a@example.com', 'hunter2hunter2')

    expect(calls[0].url).toBe(`${API_BASE_URL}/api/auth/register`)
    // 注册体里**没有** invite_code / role：注册完全开放，且客户端不能指定角色
    expect(JSON.parse(calls[0].init.body as string)).toEqual({
      email: 'a@example.com',
      password: 'hunter2hunter2',
    })
  })

  it('注册被关闭时透出后端中文文案，不自己改写', async () => {
    stubFetch({ ok: false, status: 403, json: async () => ({ detail: '注册已关闭，请联系管理员' }) })

    await expect(register('a@example.com', 'hunter2hunter2')).resolves.toEqual({
      ok: false,
      message: '注册已关闭，请联系管理员',
    })
  })

  it('邮箱已注册给出中文提示', async () => {
    stubFetch({ ok: false, status: 400, json: async () => ({ detail: 'REGISTER_USER_ALREADY_EXISTS' }) })

    await expect(register('a@example.com', 'hunter2hunter2')).resolves.toEqual({
      ok: false,
      message: '该邮箱已注册',
    })
  })

  it('密码太短时直接用后端的中文 reason', async () => {
    stubFetch({
      ok: false,
      status: 400,
      json: async () => ({
        detail: { code: 'REGISTER_INVALID_PASSWORD', reason: '密码至少需要 8 个字符' },
      }),
    })

    await expect(register('a@example.com', 'x')).resolves.toEqual({
      ok: false,
      message: '密码至少需要 8 个字符',
    })
  })

  it('邮箱格式错误的 422 英文原文映射成中文', async () => {
    stubFetch({
      ok: false,
      status: 422,
      json: async () => ({
        detail: [
          { type: 'value_error', loc: ['body', 'email'], msg: 'value is not a valid email address' },
        ],
      }),
    })

    await expect(register('bad', 'hunter2hunter2')).resolves.toEqual({
      ok: false,
      message: '邮箱格式不正确',
    })
  })
})

// ---------------------------------------------------------------------
// fetchCurrentUser
// ---------------------------------------------------------------------
describe('fetchCurrentUser', () => {
  it('未登录返回 null 而不是抛错', async () => {
    stubFetch({ ok: false, status: 401 })

    await expect(fetchCurrentUser()).resolves.toBeNull()
  })

  it('已登录返回用户信息', async () => {
    const user = { id: 'u1', email: 'a@example.com', is_active: true, is_superuser: false, is_verified: false }
    stubFetch({ ok: true, status: 200, json: async () => user })

    await expect(fetchCurrentUser()).resolves.toEqual(user)
  })

  it('探测登录态不触发全局登出回调', async () => {
    stubFetch({ ok: false, status: 401 })
    const onUnauthorized = vi.fn()
    trackListener(onUnauthorized)

    await fetchCurrentUser()

    expect(onUnauthorized).not.toHaveBeenCalled()
  })

  it('5xx 抛出错误，由调用方翻译成「无法连接服务器」而不是当成未登录', async () => {
    stubFetch({ ok: false, status: 503 })

    await expect(fetchCurrentUser()).rejects.toThrow('HTTP 503')
  })

  it('带上超时信号（挂起时会中止，不会永远 loading）', async () => {
    const { calls } = stubFetch()

    await fetchCurrentUser()

    expect(calls[0].init.signal).toBeInstanceOf(AbortSignal)
  })
})

// ---------------------------------------------------------------------
// loginWithSession：POST + /me 两步
// ---------------------------------------------------------------------
describe('loginWithSession', () => {
  const user = { id: 'u1', email: 'a@example.com', is_active: true, is_superuser: false, is_verified: false }

  it('两步都成功时返回用户并递增世代', async () => {
    const before = getAuthEpoch()
    stubFetch({ ok: true, status: 204 }, { ok: true, status: 200, json: async () => user })

    await expect(loginWithSession('a@example.com', 'hunter2hunter2')).resolves.toEqual({
      ok: true,
      user,
    })

    // 世代递增是「登录前的 401 不再能登出当前会话」的依据
    expect(getAuthEpoch()).toBe(before + 1)
  })

  it('凭据错误 → stage=credentials，且不动世代', async () => {
    const before = getAuthEpoch()
    stubFetch({ ok: false, status: 400, json: async () => ({ detail: 'LOGIN_BAD_CREDENTIALS' }) })

    await expect(loginWithSession('a@example.com', 'wrong')).resolves.toEqual({
      ok: false,
      stage: 'credentials',
      message: '邮箱或密码错误',
    })
    expect(getAuthEpoch()).toBe(before)
  })

  it('POST 成功但 /me 说未登录 → stage=session（cookie 没被接受）', async () => {
    stubFetch({ ok: true, status: 204 }, { ok: false, status: 401 })

    await expect(loginWithSession('a@example.com', 'hunter2hunter2')).resolves.toEqual({
      ok: false,
      stage: 'session',
      message: '登录未生效，请检查浏览器是否接受 cookie',
    })
  })

  it('POST 网络故障翻译成「无法连接服务器」，不是静默失败', async () => {
    stubFetch({ error: new TypeError('Failed to fetch') })

    await expect(loginWithSession('a@example.com', 'hunter2hunter2')).resolves.toEqual({
      ok: false,
      stage: 'credentials',
      message: '无法连接服务器，请检查网络后重试',
    })
  })

  it('/me 探测超时 → 超时提示 + stage=session', async () => {
    stubFetch(
      { ok: true, status: 204 },
      { error: Object.assign(new Error('已超时'), { name: 'TimeoutError' }) }
    )

    await expect(loginWithSession('a@example.com', 'hunter2hunter2')).resolves.toEqual({
      ok: false,
      stage: 'session',
      message: '请求超时，请检查网络后重试',
    })
  })
})

// ---------------------------------------------------------------------
// registerWithSession：注册成功但自动登录失败是一条死路，必须能区分
// ---------------------------------------------------------------------
describe('registerWithSession', () => {
  const user = { id: 'u1', email: 'a@example.com', is_active: true, is_superuser: false, is_verified: false }

  it('注册与自动登录都成功 → outcome=ok', async () => {
    stubFetch(
      { ok: true, status: 201 },
      { ok: true, status: 204 },
      { ok: true, status: 200, json: async () => user }
    )

    await expect(registerWithSession('a@example.com', 'hunter2hunter2')).resolves.toEqual({
      ok: true,
      outcome: 'ok',
      user,
    })
  })

  it('注册成功但自动登录失败 → outcome=login-failed，提示直接登录', async () => {
    stubFetch(
      { ok: true, status: 201 },
      { ok: false, status: 400, json: async () => ({ detail: 'LOGIN_BAD_CREDENTIALS' }) }
    )

    await expect(registerWithSession('a@example.com', 'hunter2hunter2')).resolves.toEqual({
      ok: false,
      outcome: 'login-failed',
      message: '注册已提交，请尝试直接登录',
    })
    // 自动登录失败不能被静默吞掉（用户会以为注册整体失败了）
    expect(vi.mocked(console.warn)).toHaveBeenCalled()
  })

  it('注册本身失败 → outcome=failed，透出后端提示', async () => {
    stubFetch({ ok: false, status: 400, json: async () => ({ detail: 'REGISTER_USER_ALREADY_EXISTS' }) })

    await expect(registerWithSession('a@example.com', 'hunter2hunter2')).resolves.toEqual({
      ok: false,
      outcome: 'failed',
      message: '该邮箱已注册',
    })
  })

  it('只有 login-failed 让表单切回登录模式', () => {
    expect(modeAfterRegister('login-failed')).toBe('login')
    expect(modeAfterRegister('failed')).toBe('register')
  })
})

// ---------------------------------------------------------------------
// withPending：登录的 pending 必须覆盖 /me 探测窗口
// ---------------------------------------------------------------------
describe('withPending', () => {
  it('整个动作结束才置回 false（POST 返回后按钮不能复活）', async () => {
    const events: boolean[] = []
    let releaseProbe!: () => void
    const meProbe = new Promise<void>((resolve) => {
      releaseProbe = resolve
    })

    const running = withPending(
      (pending) => events.push(pending),
      async () => {
        // 模拟登录 POST 已经返回、/me 探测还挂着的窗口
        await meProbe
        return 'done'
      }
    )

    expect(events).toEqual([true])
    releaseProbe()
    await expect(running).resolves.toBe('done')
    expect(events).toEqual([true, false])
  })

  it('动作抛错时也会解除 pending（按钮不会永久禁用）', async () => {
    const events: boolean[] = []

    await expect(
      withPending(
        (pending) => events.push(pending),
        async () => {
          throw new Error('探测超时')
        }
      )
    ).rejects.toThrow('探测超时')

    expect(events).toEqual([true, false])
  })
})

// ---------------------------------------------------------------------
// logout：失败重试一次，两次都失败才抛
// ---------------------------------------------------------------------
describe('logout', () => {
  it('第一次失败后重试，成功则 resolve', async () => {
    const { calls } = stubFetch({ ok: false, status: 500 }, { ok: true, status: 204 })

    await expect(logout()).resolves.toBeUndefined()

    expect(calls).toHaveLength(2)
    expect(calls[1].url).toBe(`${API_BASE_URL}/api/auth/logout`)
    expect(calls[1].init.method).toBe('POST')
  })

  it('两次都失败时抛错（不能把「没登出」当成登出成功）', async () => {
    const { calls } = stubFetch({ ok: false, status: 500 })

    await expect(logout()).rejects.toThrow('HTTP 500')

    expect(calls).toHaveLength(2)
  })

  it('401（会话已失效）视为登出成功：不重试、不抛错', async () => {
    const { calls } = stubFetch({ ok: false, status: 401 })

    await expect(logout()).resolves.toBeUndefined()

    // 服务端已经没有这个会话了，再试一次也是 401；重试只会拖慢登出
    expect(calls).toHaveLength(1)
  })

  it('网络错误也会重试', async () => {
    const { calls } = stubFetch({ error: new Error('offline') }, { ok: true, status: 204 })

    await expect(logout()).resolves.toBeUndefined()

    expect(calls).toHaveLength(2)
  })
})

// ---------------------------------------------------------------------
// logoutAndClearLocalData：服务端失败也要完成本地登出
// ---------------------------------------------------------------------
describe('logoutAndClearLocalData', () => {
  it('服务端确认后仍中止在途生成并清空本地数据', async () => {
    stubFetch({ ok: true, status: 204 })

    const result = await logoutAndClearLocalData()

    expect(result).toEqual({ serverConfirmed: true, error: null })
    expect(mocks.abortAllRuns).toHaveBeenCalledTimes(1)
    expect(mocks.clearAllLocalData).toHaveBeenCalledTimes(1)
    // 必须先中止在途生成再清库：反过来的话，被中止的 run 收尾时会把内容写回刚清空的库
    expect(mocks.abortAllRuns.mock.invocationCallOrder[0]).toBeLessThan(
      mocks.clearAllLocalData.mock.invocationCallOrder[0]
    )
  })

  it('服务端两次都没登出成功时，本地照清并给出「登出未确认」提示', async () => {
    stubFetch({ ok: false, status: 503 })

    const result = await logoutAndClearLocalData()

    expect(result).toEqual({
      serverConfirmed: false,
      error: '登出未确认：服务端会话可能仍有效，共享设备请关闭浏览器后重试',
    })
    expect(mocks.abortAllRuns).toHaveBeenCalledTimes(1)
    expect(mocks.clearAllLocalData).toHaveBeenCalledTimes(1)
    // 静默吞掉失败正是原来的 bug：这里必须留下错误日志
    expect(vi.mocked(console.error)).toHaveBeenCalled()
  })

  it('中止生成 / 清库抛错也不阻断登出', async () => {
    stubFetch({ ok: true, status: 204 })
    mocks.abortAllRuns.mockImplementation(() => {
      throw new Error('中止失败')
    })
    mocks.clearAllLocalData.mockRejectedValue(new Error('QuotaExceededError'))

    const result = await logoutAndClearLocalData()

    expect(result).toEqual({ serverConfirmed: true, error: null })
    expect(vi.mocked(console.error)).toHaveBeenCalledTimes(2)
  })
})

// ---------------------------------------------------------------------
// describeAuthError
// ---------------------------------------------------------------------
describe('describeAuthError', () => {
  it('翻译已知错误码', () => {
    expect(describeAuthError('LOGIN_BAD_CREDENTIALS')).toBe('邮箱或密码错误')
  })

  it('没有 detail 时退回状态码', () => {
    expect(describeAuthError('', 502)).toBe('请求失败（HTTP 502）')
  })

  it('常见 422 英文校验说明映射成中文', () => {
    expect(describeAuthError('email: value is not a valid email address', 422)).toBe('邮箱格式不正确')
    expect(describeAuthError('password: Field required', 422)).toBe('密码为必填项')
  })

  it('后端给的中文说明原样透出', () => {
    expect(describeAuthError('密码至少需要 8 个字符')).toBe('密码至少需要 8 个字符')
  })

  it('未知内容补中文前缀，但原文保留（不吞可诊断信息）', () => {
    expect(describeAuthError('SOME_NEW_CODE')).toBe('认证请求失败：SOME_NEW_CODE')
  })
})

// ---------------------------------------------------------------------
// describeConnectionError
// ---------------------------------------------------------------------
describe('describeConnectionError', () => {
  it('超时（AbortError / TimeoutError）给出超时提示', () => {
    expect(describeConnectionError(Object.assign(new Error('x'), { name: 'AbortError' }))).toBe(
      '请求超时，请检查网络后重试'
    )
    expect(describeConnectionError(Object.assign(new Error('x'), { name: 'TimeoutError' }))).toBe(
      '请求超时，请检查网络后重试'
    )
  })

  it('其余失败给出「无法连接服务器」', () => {
    expect(describeConnectionError(new Error('HTTP 500'))).toBe('无法连接服务器，请检查网络后重试')
    expect(describeConnectionError(undefined)).toBe('无法连接服务器，请检查网络后重试')
  })
})
