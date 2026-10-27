/**
 * 管理后台的认证接口客户端。
 *
 * 与用户端 `frontend/src/lib/auth.ts` 的分工不同：这里没有注册、改密、本地库认领——
 * 管理后台只做「登录 → 确认角色 → 用完登出」一件小事。不持有 React 状态，状态在
 * `App.tsx`；放 lib 层是为了能在 node 环境的 vitest 里直接测。
 *
 * 后端对应实现：`app/api/auth.py`。会话是 HttpOnly cookie（默认 `bennett_session`），
 * 与用户端**共享同一个会话**：在用户端登录过且角色够，打开管理后台就是已登录状态。
 */

import { apiFetch, readErrorDetail } from './api';
import { ROLE_LABELS } from './admin';

/** 当前登录用户。字段与后端 `UserRead` 对齐（snake_case 是线上契约）。 */
export interface AuthUser {
  id: string;
  email: string;
  is_active: boolean;
  is_superuser: boolean;
  is_verified: boolean;
  /**
   * 角色：`user` / `admin` / `superadmin`（见后端 `app/roles.py`），是能否使用管理后台的
   * 唯一依据；`is_superuser` 只是它 `>= admin` 的派生镜像，后端鉴权也只读 role。
   */
  role: string;
}

/**
 * 使用管理后台的最低角色。与后端 `ADMIN_LEVEL` 对齐。
 *
 * 用集合而不是 `role === 'admin'`：`superadmin` 权限严格包含 `admin` 的全部能力，
 * 写成等号会把最高一档漏掉（表现为「超管登录后被挡在门外」）。
 */
const ADMIN_ROLES = new Set(['admin', 'superadmin']);

/** 能否使用管理后台。权限的最终裁决在后端（403），这里只决定界面走向。 */
export function canAccessAdmin(user: AuthUser | null): boolean {
  return user !== null && ADMIN_ROLES.has(user.role);
}

/**
 * 顶栏菜单里展示的角色名。
 *
 * 从 `lib/admin` 取同一份，不在这里再写一遍：两张表漂移时会出现「顶栏说超管、
 * 用户表说管理员」——同一个人在同一个页面上有两种叫法。
 */
export function roleLabelOf(user: AuthUser | null): string {
  if (!user) return '未登录';
  return ROLE_LABELS[user.role] ?? user.role;
}

/** 是否是最高一档（决定「用户管理」里能否改角色）。 */
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
 * 认证业务错误码 → 用户能懂的文案。
 *
 * 管理后台只会碰到登录相关的一小撮码；全量映射在用户端 `frontend/src/lib/auth.ts`。
 * 中文 detail（后端的 reason 字段）原样透出，其余兜底保留原文便于诊断。
 */
export function describeAuthError(detail: string, status?: number): string {
  if (!detail) return status ? `请求失败（HTTP ${status}）` : '请求失败，请稍后重试';
  if (detail === 'LOGIN_BAD_CREDENTIALS') return '邮箱或密码错误';
  if (/[\u4e00-\u9fff]/.test(detail)) return detail;
  return `认证请求失败：${detail}`;
}

/**
 * 取当前登录用户。
 *
 * 未登录返回 `null`：启动时探测登录态本来就会拿到 401，这是**预期结果而非错误**，
 * 所以调用方不应把它当异常打日志。
 */
export async function fetchCurrentUser(): Promise<AuthUser | null> {
  const response = await apiFetch('/api/auth/me', { timeoutMs: AUTH_TIMEOUT_MS });

  if (response.status === 401) return null;
  if (!response.ok) throw new Error(`HTTP ${response.status}`);

  return (await response.json()) as AuthUser;
}

export type LoginAttempt =
  | { ok: true; user: AuthUser }
  /**
   * `stage` 区分失败发生在哪一步：
   *   - `credentials`：登录请求本身失败，用户还没建立起任何会话；
   *   - `session`：POST 成功但会话没建立（cookie 没被接受、探测网络失败）。
   */
  | { ok: false; stage: 'credentials' | 'session'; message: string };

/**
 * 登录的完整流程：POST 之后还要探测一次 `/me`。
 *
 * **必须用表单编码**：后端复用 `OAuth2PasswordRequestForm`，字段名是 `username`
 * （填邮箱）而不是 `email`。发 JSON 会得到 422，而不是「密码错误」这种有意义的提示。
 *
 * 登录接口只回 204（凭据是 cookie 本身），不探测就不知道「登录到底有没有生效」。
 * 两步必须包在同一个 pending 窗口里（见 `App.tsx`），否则连点会发出第二次登录——
 * 后端每次登录都新建一条会话记录。
 */
export async function login(email: string, password: string): Promise<LoginAttempt> {
  let response: Response;
  try {
    response = await apiFetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ username: email, password }),
      timeoutMs: AUTH_TIMEOUT_MS,
    });
  } catch (err) {
    console.error('登录请求失败:', err);
    return { ok: false, stage: 'credentials', message: describeConnectionError(err) };
  }

  if (!response.ok) {
    return {
      ok: false,
      stage: 'credentials',
      message: describeAuthError(await readErrorDetail(response), response.status),
    };
  }

  try {
    const user = await fetchCurrentUser();
    if (!user) {
      // 登录接口说成功、但探测说未登录：cookie 没被浏览器接受（例如
      // AUTH_COOKIE_SECURE=true 却在 http 下访问）。宁可让用户重试，
      // 也不要进到「已登录但不知道是谁」的半残状态。
      return { ok: false, stage: 'session', message: '登录未生效，请检查浏览器是否接受 cookie' };
    }
    return { ok: true, user };
  } catch (err) {
    console.error('登录成功但获取用户信息失败:', err);
    return { ok: false, stage: 'session', message: describeConnectionError(err) };
  }
}

/**
 * 登出。服务端会删掉会话记录，因此这是**真实失效**，不只是清本地 cookie。
 *
 * 失败重试一次：凭据是 HttpOnly cookie，用户没法手动清，服务端会话没删掉就意味着
 * 刷新页面又「自动登录」回来。两次都失败时返回未确认结果，由界面明确提示，
 * 而不是默默把用户踢回登录页造成「已经退出」的错觉。
 */
export async function logout(): Promise<AuthResult> {
  const attempt = async (): Promise<void> => {
    const response = await apiFetch('/api/auth/logout', { timeoutMs: AUTH_TIMEOUT_MS });
    // 非 2xx 也算失败：服务端 500（会话没删）被当成登出成功，用户会以为已经退出。
    // 401 是例外——服务端会话已经不存在，这正是登出想要的结果。
    if (!response.ok && response.status !== 401) {
      throw new Error(`HTTP ${response.status}`);
    }
  };

  try {
    await attempt();
  } catch (err) {
    console.warn('登出请求失败，重试一次:', err);
    try {
      await attempt();
    } catch (retryErr) {
      console.error('登出仍未确认:', retryErr);
      return { ok: false, message: '登出未确认：服务端会话可能仍有效，请关闭浏览器后重试' };
    }
  }

  return { ok: true };
}
