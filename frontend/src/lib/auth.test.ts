import { afterEach, describe, expect, it, vi } from 'vitest'

import { API_BASE_URL } from './constants'
import { apiFetch, readErrorDetail, setUnauthorizedListener } from './api'
import { describeAuthError, fetchCurrentUser, login, logout, register } from './auth'

interface StubResponseInit {
  ok?: boolean
  status?: number
  json?: () => Promise<unknown>
}

/**
 * 记录每次调用的 (url, init)。
 *
 * 比事后读 `fetchMock.mock.calls` 好：那种写法在 `vi.fn` 的实现签名上很容易撞到
 * 类型推断的坑，而这里断言请求形状只需要这两个字段。
 */
function stubFetch({ ok = true, status = 200, json }: StubResponseInit = {}) {
  const calls: Array<{ url: string; init: RequestInit }> = []
  const fetchMock = vi.fn(async (url: string, init: RequestInit) => {
    calls.push({ url, init })
    return { ok, status, json: json ?? (async () => ({})) }
  })
  vi.stubGlobal('fetch', fetchMock)
  return { fetchMock, calls }
}

const asResponse = (json: () => Promise<unknown>) => ({ json }) as unknown as Response

afterEach(() => {
  vi.unstubAllGlobals()
  // 监听器是模块级状态，用例之间必须隔离
  setUnauthorizedListener(null)
})

// ---------------------------------------------------------------------
// apiFetch：全局 401 处理
// ---------------------------------------------------------------------
describe('apiFetch', () => {
  it('401 触发全局登出回调', async () => {
    stubFetch({ ok: false, status: 401 })
    const onUnauthorized = vi.fn()
    setUnauthorizedListener(onUnauthorized)

    await apiFetch('/api/models')

    expect(onUnauthorized).toHaveBeenCalledOnce()
  })

  it('notifyUnauthorized=false 时不触发（401 本身就是预期结果）', async () => {
    stubFetch({ ok: false, status: 401 })
    const onUnauthorized = vi.fn()
    setUnauthorizedListener(onUnauthorized)

    await apiFetch('/api/auth/me', { notifyUnauthorized: false })

    expect(onUnauthorized).not.toHaveBeenCalled()
  })

  it('非 401 不触发回调', async () => {
    stubFetch({ ok: false, status: 500 })
    const onUnauthorized = vi.fn()
    setUnauthorizedListener(onUnauthorized)

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
    setUnauthorizedListener(onUnauthorized)

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
    expect(JSON.parse(calls[0].init.body as string)).toEqual({
      email: 'a@example.com',
      password: 'hunter2hunter2',
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

  it('邮箱格式错误时带上字段路径', async () => {
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
      message: 'email: value is not a valid email address',
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
    setUnauthorizedListener(onUnauthorized)

    await fetchCurrentUser()

    expect(onUnauthorized).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------
// logout
// ---------------------------------------------------------------------
describe('logout', () => {
  it('POST 到 /api/auth/logout', async () => {
    const { calls } = stubFetch({ ok: true, status: 204 })

    await logout()

    expect(calls[0].url).toBe(`${API_BASE_URL}/api/auth/logout`)
    expect(calls[0].init.method).toBe('POST')
  })
})

// ---------------------------------------------------------------------
// describeAuthError
// ---------------------------------------------------------------------
describe('describeAuthError', () => {
  it('翻译已知错误码', () => {
    expect(describeAuthError('LOGIN_BAD_CREDENTIALS')).toBe('邮箱或密码错误')
  })

  it('未知内容原样透出，不吞掉可诊断信息', () => {
    expect(describeAuthError('SOME_NEW_CODE')).toBe('SOME_NEW_CODE')
  })

  it('没有 detail 时退回状态码', () => {
    expect(describeAuthError('', 502)).toBe('请求失败（HTTP 502）')
  })
})
