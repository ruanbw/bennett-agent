/**
 * 统一请求层：基址拼接、cookie 携带、401 的全局处理、超时。
 *
 * ## 为什么需要它
 *
 * 认证引入后任何请求都可能因「会话过期」返回 401，而 401 的正确反应是**全局的**
 * （退回登录页），不是每个调用点各自处理。此前 `App.tsx`、`chat-transport.ts`、
 * `ChatProvider.tsx` 三处各写一遍 `fetch`，加上 401 判断就是三份重复逻辑，而且
 * 以后新增调用点很容易漏掉。
 *
 * ## 为什么用模块级回调而不是 React Context
 *
 * `chat-transport.ts` 是纯模块（在 node 环境的 vitest 里直接测），不能依赖 React
 * Context。模块级监听器让纯模块也能参与全局 401 处理，同时保持可测。
 *
 * ## 401 监听是一组，不是一个
 *
 * 此前是单槽回调：后来者直接覆盖先注册的那个。两个组件同时挂载时（或 React
 * StrictMode 的双挂载之间）必然丢掉一个监听者，表现为「有时 401 不退回登录页」。
 * 改成 Set 后每个监听者各自增删，互不影响。
 *
 * ## 世代（epoch）守卫
 *
 * 请求发出时记下当时的世代；401 回来时只有世代仍然一致才通知监听者。登录前发出的
 * 请求可能带着旧 cookie 在登录成功**之后**才拿到 401，那条 401 说的已经是过去的事，
 * 通知出去会把刚建立的登录态打回登录页（用户看到「登录成功又立刻被登出」）。
 */

import { API_BASE_URL } from './constants';

/** 会话失效（401）时的通知。参数是该请求所属的认证世代。 */
export type UnauthorizedListener = (epoch: number) => void;

const unauthorizedListeners = new Set<UnauthorizedListener>();

/**
 * 当前认证世代，只增不减。
 *
 * 登录/注册成功（会话真正建立）时递增：那一瞬间起，此前发出的请求的结果都不再能
 * 代表当前登录态。
 */
let authEpoch = 0;

export function getAuthEpoch(): number {
  return authEpoch;
}

/** 会话建立（登录/注册成功）时调用；返回新世代。 */
export function bumpAuthEpoch(): number {
  authEpoch += 1;
  return authEpoch;
}

/** 注册 401 监听。注销时必须传入同一个函数引用（见 `removeUnauthorizedListener`）。 */
export function addUnauthorizedListener(listener: UnauthorizedListener): void {
  unauthorizedListeners.add(listener);
}

/** 注销一个 401 监听。注销未注册过的函数是空操作。 */
export function removeUnauthorizedListener(listener: UnauthorizedListener): void {
  unauthorizedListeners.delete(listener);
}

export interface ApiFetchOptions extends RequestInit {
  /**
   * 401 时是否触发全局登出回调，默认 `true`。
   *
   * 需要关掉的场合是「401 本身就是预期结果」：启动时探测登录态、以及登录接口
   * 自身的失败——这两处再触发一次「会话失效」没有意义，只会多绕一圈。
   */
  notifyUnauthorized?: boolean;

  /**
   * 等响应头的下限（毫秒）。到点中止请求，`fetch` 以 `TimeoutError` 拒绝。
   *
   * 认证请求必须带它：`/me` 探测挂起会让登录页永远停在 loading、登录按钮永远禁用。
   * 调用方自带 `signal` 时**两者都要生效**：用户取消与超时是两条独立的退出路径，
   * 少任何一条都会挂住（合成方式见 `connectGuard`）。
   */
  timeoutMs?: number;
}

/**
 * 等响应头超时时给中止用的说明。
 *
 * 它必须与「连不上」在错误层面就分开：连不上是 `fetch` 自己抛 `TypeError`（文案各
 * 浏览器不同，Chrome 是「Failed to fetch」），到点是我们主动中止并带 `TimeoutError`。
 * 下游据此翻成两句不同的中文（`connection-failure.ts` / `auth.ts` 的「请求超时」
 * 对「网络连接中断」）——这两种失败下一步动作不同：一个再等一等/重试，一个要查网络。
 */
export const CONNECT_TIMEOUT_MESSAGE = '等待服务器响应超时：请求已发出，但没有等到响应';

/** 「等响应头」这一跳的守卫：一个专用 controller，超时与用户取消都汇到它身上。 */
interface ConnectGuard {
  signal: AbortSignal;
  /** 响应头到手后调用：停表。 */
  disarm: () => void;
}

function connectGuard(ms: number, caller: AbortSignal | null | undefined): ConnectGuard {
  const controller = new AbortController();

  /*
    与调用方 signal 的联动**不解除**，哪怕响应头已经到手。

    fetch 的 signal 管的不只是建连，还有响应体：中途把监听摘掉，用户点「停止」时
    响应体不会 reject，`reader.read()` 就永远挂着——run 停不下来，后端继续生成、
    继续计费。监听器挂在调用方的 controller 上（对话里是每轮一个），随这一轮一起
    回收，不会跨轮累积。
  */
  if (caller) {
    if (caller.aborted) controller.abort(caller.reason);
    else caller.addEventListener('abort', () => controller.abort(caller.reason), { once: true });
  }

  // 中止理由刻意用 TimeoutError 而不是默认的 AbortError：AbortError 在对话链路里
  // 是「用户点了停止」（chat-runs 据此把这一轮标成已停止）。超时被记成用户主动停止，
  // 用户就只会看到一句「已停止」，没人知道是服务器没响应。
  const timer = setTimeout(
    () => controller.abort(new DOMException(CONNECT_TIMEOUT_MESSAGE, 'TimeoutError')),
    ms
  );

  return { signal: controller.signal, disarm: () => clearTimeout(timer) };
}

/**
 * 发起请求。
 *
 * `credentials: 'include'` 在**同源**（Vite 代理 / 生产反代）下与默认行为一致
 * （cookie 照常带上），在**跨域**下则是唯一会带上 cookie 的模式。所以它对两种部署
 * 都正确——前提是跨域时后端配了 `allow_credentials=True`，见 `app/server.py`。
 */
export async function apiFetch(path: string, options: ApiFetchOptions = {}): Promise<Response> {
  const { notifyUnauthorized = true, timeoutMs, ...init } = options;
  const epoch = authEpoch;

  /*
    计时只覆盖「请求发出去 → 响应头回来」这一跳，所以拿到 response 就必须停表：
    响应体怎么读由调用方自己管（对话流另有一道 60s 空闲看门狗）。

    不用 `AbortSignal.timeout` 的原因有两条：它的计时**不可提前取消**（响应头 200ms
    就回来了，那颗定时器仍会在 15s 后把早就不用了的 signal 中止），而且它没法与调用方
    的 signal 合成同一条退出路径——旧写法在两者同时给的场景下直接把 timeoutMs 丢掉，
    于是「请求发出去、响应头永远不回来」这一跳没有任何上界。
  */
  const guard = timeoutMs ? connectGuard(timeoutMs, init.signal) : null;

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      credentials: 'include',
      ...init,
      signal: guard ? guard.signal : init.signal,
    });
  } finally {
    guard?.disarm();
  }

  // 世代对不上：这是登录前发出的请求，它的 401 属于旧会话，不能拿来登出当前会话
  if (response.status === 401 && notifyUnauthorized && epoch === authEpoch) {
    // 拷贝一份再遍历：监听者可能在回调里注销自己，直接遍历 Set 会跳过下一个
    for (const listener of [...unauthorizedListeners]) listener(epoch);
  }

  return response;
}

/**
 * 从错误响应里提取可读说明。
 *
 * 三种 `detail` 形状都要处理，否则用户只会看到「HTTP 错误: 422」而不知道该改什么：
 *
 *   - 字符串：业务错误码，如 `LOGIN_BAD_CREDENTIALS`；
 *   - 数组：FastAPI 的请求校验错误，`loc` 形如 `['body','messages',0,'content']`；
 *   - 对象：fastapi-users 的 `{code, reason}`，注册密码不合规走这条。
 *
 * 取不到时返回空串，由调用方退回状态码——**不编造文案**。
 */
export async function readErrorDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown };
    const detail = body?.detail;

    if (typeof detail === 'string') return detail;

    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { loc?: unknown; msg?: unknown } | undefined;
      // 去掉首段的 'body'，剩下的就是用户能认的字段路径
      const where = Array.isArray(first?.loc) ? first.loc.slice(1).join('.') : '';
      const message = typeof first?.msg === 'string' ? first.msg : '';
      return where ? `${where}: ${message}` : message;
    }

    if (detail && typeof detail === 'object') {
      const { code, reason } = detail as { code?: unknown; reason?: unknown };
      // reason 是写给人看的说明，code 只是标识；有 reason 就别拿 code 顶替
      if (typeof reason === 'string' && reason) return reason;
      if (typeof code === 'string' && code) return code;
    }

    return '';
  } catch {
    // 响应体不是 JSON（例如反向代理返回 HTML 错误页）：退回状态码
    return '';
  }
}
