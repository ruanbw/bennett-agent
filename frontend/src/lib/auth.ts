/**
 * 认证接口客户端与认证动作编排。
 *
 * 只做「调用 + 归一化结果 + 时序编排」，不持有 React 状态——状态在 `use-auth.ts`。
 * 放在 lib 层是为了能直接测：本仓库的 vitest 跑在 node 环境，没有 DOM。
 *
 * 后端对应实现：`app/api/auth.py`。
 */

import { clearAllLocalData } from '../db';
import { apiFetch, bumpAuthEpoch, readErrorDetail } from './api';
import { abortAllRuns } from './chat-runs';
import { flushPending, stopSync } from './sync/engine';

/** 当前登录用户。字段与后端 `UserRead` 对齐（snake_case 是线上契约）。 */
export interface AuthUser {
  id: string;
  email: string;
  is_active: boolean;
  is_superuser: boolean;
  is_verified: boolean;
  /**
   * 角色：`user` / `admin` / `superadmin`（见后端 `app/roles.py`），是能否进管理后台的
   * 唯一依据；`is_superuser` 只是它 `>= admin` 的派生镜像，后端鉴权也只读 role。
   */
  role: string;
}

/**
 * 进管理后台的最低角色。与后端 `ADMIN_LEVEL` 对齐。
 *
 * 用集合而不是 `role === 'admin'`：`superadmin` 权限严格包含 `admin` 的全部能力，
 * 写成等号会把最高一档漏掉（表现为「超管看不到后台入口」）。
 */
const ADMIN_ROLES = new Set(['admin', 'superadmin']);

/** 能否显示管理后台入口。权限的最终裁决在后端（403），这里只决定入口可见性。 */
export function canAccessAdmin(user: AuthUser | null): boolean {
  return user !== null && ADMIN_ROLES.has(user.role);
}

/** 是否是最高一档（只有它能看到角色设置按钮）。 */
export function isSuperadmin(user: AuthUser | null): boolean {
  return user?.role === 'superadmin';
}

export type AuthResult = { ok: true } | { ok: false; message: string };

/**
 * 认证请求超时。
 *
 * 没有超时的话：启动探测挂在半路会让登录页永远停在 loading，登录请求挂起会让按钮
 * 永远禁用（连重试的入口都没有）。15s 够慢网络下的表单提交，又不至于像卡死。
 */
export const AUTH_TIMEOUT_MS = 15_000;

/** 服务器不可达（网络故障、5xx、超时）时的统一提示：与「未登录」是两回事。 */
export const SERVER_UNREACHABLE_MESSAGE = '无法连接服务器，请检查网络后重试';

/** 请求超时（AbortSignal.timeout 触发）时的提示。 */
export const REQUEST_TIMEOUT_MESSAGE = '请求超时，请检查网络后重试';

/** 登出请求两次都没成功：本地已退出，但服务端会话可能仍然有效。 */
export const LOGOUT_UNCONFIRMED_MESSAGE =
  '登出未确认：服务端会话可能仍有效，共享设备请关闭浏览器后重试';

/** 账号已创建、但紧接着的自动登录没成功时给用户的下一步指示。 */
export const REGISTER_LOGIN_FAILED_MESSAGE = '账号已创建，请直接登录';

/**
 * 密码最小长度，与后端 `app/services/auth.py:MIN_PASSWORD_LENGTH` 保持一致。
 *
 * 前端校验只是为了**即时反馈**，不是安全边界——真正的校验必须在服务端，因为
 * 客户端的一切都可以绕过。两边都留着是有意的：前端给体验，后端给保证。
 */
export const MIN_PASSWORD_LENGTH = 8;

/** 判断一个错误是不是「中断/超时」：AbortSignal.timeout 抛的 name 是 TimeoutError。 */
function isAbortError(err: unknown): boolean {
  const name = (err as { name?: unknown } | null)?.name;
  return name === 'AbortError' || name === 'TimeoutError';
}

/**
 * 把请求异常翻译成用户能懂的说明。
 *
 * 只区分「超时」与「连不上/服务端出错」两种：两者的下一步动作都是检查网络后重试，
 * 但说清是哪一种能省掉一轮「我网络没问题啊」的困惑。
 */
export function describeConnectionError(err: unknown): string {
  return isAbortError(err) ? REQUEST_TIMEOUT_MESSAGE : SERVER_UNREACHABLE_MESSAGE;
}

/**
 * 取当前登录用户。
 *
 * 未登录返回 `null`：启动时探测登录态本来就会拿到 401，这是**预期结果而非错误**，
 * 所以关掉 401 的全局通知，也不抛异常。
 */
export async function fetchCurrentUser(): Promise<AuthUser | null> {
  const response = await apiFetch('/api/auth/me', {
    notifyUnauthorized: false,
    timeoutMs: AUTH_TIMEOUT_MS,
  });

  if (response.status === 401) return null;
  if (!response.ok) throw new Error(`HTTP ${response.status}`);

  return (await response.json()) as AuthUser;
}

/**
 * 登录。
 *
 * **必须用表单编码**：后端复用 `OAuth2PasswordRequestForm`，字段名是 `username`
 * （填邮箱）而不是 `email`。发 JSON 会得到 422，而不是「密码错误」这种有意义的提示。
 *
 * 成功返回 204（无响应体）——凭据是 cookie 本身，不在响应体里。
 */
export async function login(email: string, password: string): Promise<AuthResult> {
  const response = await apiFetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ username: email, password }),
    notifyUnauthorized: false,
    timeoutMs: AUTH_TIMEOUT_MS,
  });

  if (response.ok) return { ok: true };

  return { ok: false, message: describeAuthError(await readErrorDetail(response), response.status) };
}

/** 注册。成功返回 201，但**不会自动登录**——调用方需要再走一次 `login`。
 *
 * 注册完全开放，只需邮箱 + 密码；建出来的账号是普通用户（`role=user`）。
 * 权限只能由超管在管理后台授予，不存在自助提权的路径。
 */
export async function register(email: string, password: string): Promise<AuthResult> {
  const response = await apiFetch('/api/auth/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
    notifyUnauthorized: false,
    timeoutMs: AUTH_TIMEOUT_MS,
  });

  if (response.ok) return { ok: true };

  return { ok: false, message: describeAuthError(await readErrorDetail(response), response.status) };
}

/**
 * 登出。服务端会删掉会话记录，因此这是**真实失效**，不只是清本地 cookie。
 *
 * 失败重试一次：凭据是 HttpOnly cookie，用户没法手动清，服务端会话没删掉就意味着
 * 刷新页面又「自动登录」回来。一次重试能挡掉网络抖动与瞬时 5xx；两次都失败则抛错，
 * 由调用方决定怎么提示（见 `logoutAndClearLocalData`）。
 */
export async function logout(): Promise<void> {
  try {
    await requestLogout();
  } catch (err) {
    // 401 说明服务端会话已经不存在：这正是登出想要的结果，没必要再试或报警。
    if (isUnauthorizedError(err)) return;
    console.warn('登出请求失败，重试一次:', err);
    try {
      await requestLogout();
    } catch (retryErr) {
      if (isUnauthorizedError(retryErr)) return;
      throw retryErr;
    }
  }
}

function isUnauthorizedError(err: unknown): boolean {
  return typeof err === 'object' && err !== null && (err as { status?: unknown }).status === 401;
}

async function requestLogout(): Promise<void> {
  const response = await apiFetch('/api/auth/logout', {
    method: 'POST',
    notifyUnauthorized: false,
    timeoutMs: AUTH_TIMEOUT_MS,
  });

  // 非 2xx 也算失败：服务端 500（会话没删）被当成登出成功，用户会以为已经退出
  if (!response.ok) {
    throw Object.assign(new Error(`HTTP ${response.status}`), { status: response.status });
  }
}

// ---------------------------------------------------------------------
// 动作编排
// ---------------------------------------------------------------------
export type LoginAttempt =
  | { ok: true; user: AuthUser }
  /**
   * `stage` 区分失败发生在哪一步：
   *   - `credentials`：登录请求本身失败，用户还没建立起任何会话；
   *   - `session`：POST 成功但会话没建立（cookie 没被接受、探测网络失败），
   *     调用方必须把状态明确退回未登录，不能停在「已登录但不知道是谁」。
   */
  | { ok: false; stage: 'credentials' | 'session'; message: string };

/**
 * 登录的完整流程：POST 之后还要探测一次 `/me`。
 *
 * 登录接口只回 204（凭据是 cookie 本身），不探测就不知道「登录到底有没有生效」。
 * 两步必须包在同一个 pending 窗口里（见 `use-auth.ts` 的 `login`）。
 */
export async function loginWithSession(email: string, password: string): Promise<LoginAttempt> {
  let result: AuthResult;
  try {
    result = await login(email, password);
  } catch (err) {
    console.error('登录请求失败:', err);
    return { ok: false, stage: 'credentials', message: describeConnectionError(err) };
  }

  if (!result.ok) return { ok: false, stage: 'credentials', message: result.message };

  try {
    const user = await fetchCurrentUser();
    if (!user) {
      // 登录接口说成功、但探测说未登录：cookie 没被浏览器接受（例如
      // AUTH_COOKIE_SECURE=true 却在 http 下访问）。宁可让用户重试，
      // 也不要进到「已登录但不知道是谁」的半残状态。
      return { ok: false, stage: 'session', message: '登录未生效，请检查浏览器是否接受 cookie' };
    }

    // 会话真正建立：从此之后，登录前发出的请求拿到的 401 都不再代表当前登录态
    bumpAuthEpoch();
    return { ok: true, user };
  } catch (err) {
    console.error('登录成功但获取用户信息失败:', err);
    return { ok: false, stage: 'session', message: describeConnectionError(err) };
  }
}

export type RegisterOutcome = 'ok' | 'login-failed' | 'failed';

/**
 * 注册结果 → 表单接下来停在哪个模式（`LoginPage` 用）。
 *
 * 只有 `login-failed` 要换模式：账号已建好，留在注册模式再点一次只会拿到「该邮箱已
 * 注册」。抽成纯函数是为了能在 node 环境测住这个分支。
 */
export function modeAfterRegister(outcome: RegisterOutcome): 'login' | 'register' {
  return outcome === 'login-failed' ? 'login' : 'register';
}

export type RegisterAttempt =
  | { ok: true; outcome: 'ok'; user: AuthUser }
  | { ok: false; outcome: 'failed' | 'login-failed'; message: string };

/**
 * 注册并自动登录。
 *
 * 「注册成功但自动登录失败」必须与「注册失败」区分开：前者账号已经存在，要把用户
 * 带回登录模式直接登录（模式切换见 `modeAfterRegister`）。
 */
export async function registerWithSession(
  email: string,
  password: string
): Promise<RegisterAttempt> {
  let result: AuthResult;
  try {
    result = await register(email, password);
  } catch (err) {
    console.error('注册请求失败:', err);
    return { ok: false, outcome: 'failed', message: describeConnectionError(err) };
  }

  if (!result.ok) return { ok: false, outcome: 'failed', message: result.message };

  // 后端注册接口不下发 cookie，紧接着登一次，省掉用户再手填一遍密码
  const attempt = await loginWithSession(email, password);
  if (attempt.ok) return { ok: true, outcome: 'ok', user: attempt.user };

  console.warn('账号已创建，但自动登录失败:', attempt.message);
  return { ok: false, outcome: 'login-failed', message: REGISTER_LOGIN_FAILED_MESSAGE };
}

export interface LogoutResult {
  /** 服务端是否确认会话已失效。 */
  serverConfirmed: boolean;
  /** 未确认时给用户看的说明；已确认时为 `null`。 */
  error: string | null;
}

/**
 * 登出编排：在途生成 + 服务端会话 + 本地数据，顺序是掐生成 → 失效会话 → 清本地库。
 *
 * 三步互不阻塞：任一步失败都只记日志，绝不把用户卡在「登出不了」的状态。服务端没确认
 * 时要把这件事**明确告知**——cookie 还在，刷新就会恢复登录，共享设备上用户必须知道
 * 要关掉浏览器。本地数据无论如何都清：留着就是跨账号泄漏。
 */
export async function logoutAndClearLocalData(): Promise<LogoutResult> {
  try {
    abortAllRuns();
  } catch (err) {
    console.error('中止在途生成失败:', err);
  }

  // 顺序不能换：**先推再登出**。后端登出 = 删掉 access_token 行，之后任何 push 都是
  // 401；反过来写就等于「登出前的最后一次同步」永远失败，还要多触发一次全局 401。
  // 推不上去也照常登出（不能把用户卡住），但要在日志里留下痕迹。
  try {
    const remaining = await flushPending(2_000);
    if (remaining > 0) {
      console.warn(`登出时仍有 ${remaining} 条内容未同步到服务器`);
    }
  } catch (err) {
    console.warn('登出前的最后一次同步失败:', err);
  }

  let serverConfirmed = false;
  try {
    await logout();
    serverConfirmed = true;
  } catch (err) {
    console.error('登出请求两次都失败，服务端会话可能仍然有效:', err);
  }
  stopSync();

  try {
    await clearAllLocalData();
  } catch (err) {
    console.error('清理本地会话数据失败:', err);
  }

  return {
    serverConfirmed,
    error: serverConfirmed ? null : LOGOUT_UNCONFIRMED_MESSAGE,
  };
}

/**
 * 把一次异步动作包进「占用中」窗口：动作开始前置 `true`，**整个动作**结束后才置 `false`。
 *
 * 抽到 lib 层的唯一目的是可测：hook 的渲染测试在本仓库没有环境（vitest 是 node，
 * 没有 jsdom），时序不变量只能在这里锁住。
 */
export async function withPending<T>(
  setPending: (pending: boolean) => void,
  action: () => Promise<T>
): Promise<T> {
  setPending(true);
  try {
    return await action();
  } finally {
    setPending(false);
  }
}

/**
 * 后端错误码 → 用户可读文案。
 *
 * 后端返回的是**错误码**（如 `LOGIN_BAD_CREDENTIALS`），直接显示毫无意义。
 */
const AUTH_ERROR_MESSAGES: Record<string, string> = {
  LOGIN_BAD_CREDENTIALS: '邮箱或密码错误',
  REGISTER_USER_ALREADY_EXISTS: '该邮箱已注册',
  REGISTER_INVALID_PASSWORD: `密码不符合要求（至少 ${MIN_PASSWORD_LENGTH} 个字符）`,
};

/** 字段路径里常见的字段名 → 中文。取不到就保留英文路径，至少能定位。 */
const FIELD_LABELS: Record<string, string> = {
  email: '邮箱',
  username: '邮箱',
  password: '密码',
  new_password: '新密码',
};

/**
 * 常见 422 校验原文 → 中文说明（`$1` 用正则第一个捕获组填充）。
 *
 * 匹配的是**片段**而不是整句：pydantic 的 `msg` 会带上下文，例如
 * `value is not a valid email address: An email address must have an @-sign.`，
 * 精确等值匹配基本命中不了。
 */
const VALIDATION_MESSAGES: Array<[RegExp, string]> = [
  [/not a valid email|must have an @-sign/i, '格式不正确'],
  [/field required/i, '为必填项'],
  [/at least (\d+) character/i, '至少需要 $1 个字符'],
  [/at most (\d+) character/i, '最多 $1 个字符'],
];

/** 拆 `readErrorDetail` 数组分支拼出的 `字段路径: 原文`。字段路径本身不含冒号。 */
function splitFieldPath(detail: string): { field: string; message: string } {
  const index = detail.indexOf(': ');
  if (index === -1) return { field: '', message: detail };
  return { field: detail.slice(0, index), message: detail.slice(index + 2) };
}

function mapValidationMessage(message: string): string | null {
  for (const [pattern, template] of VALIDATION_MESSAGES) {
    const match = pattern.exec(message);
    if (match) return template.replace('$1', match[1] ?? '');
  }
  return null;
}

export function describeAuthError(detail: string, status?: number): string {
  if (!detail) return status ? `请求失败（HTTP ${status}）` : '请求失败，请稍后重试';

  const known = AUTH_ERROR_MESSAGES[detail];
  if (known) return known;

  const { field, message } = splitFieldPath(detail);
  const mapped = mapValidationMessage(message);
  if (mapped) {
    const label = FIELD_LABELS[field];
    if (label) return `${label}${mapped}`;
    return field ? `${field}: ${mapped}` : mapped;
  }

  // 后端 reason 这类本来就是中文说明（如密码太短）：直接用，不要套前缀
  if (/[\u4e00-\u9fff]/.test(detail)) return detail;

  // 兜底：保留原文（可诊断），但补一句中文说明，免得用户只看到一句英文
  return `认证请求失败：${detail}`;
}
