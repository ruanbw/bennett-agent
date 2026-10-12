/**
 * 认证接口客户端。
 *
 * 只做「调用 + 归一化结果」，不持有 React 状态——状态在 `use-auth.ts`。这样表单编码、
 * 错误码翻译这类容易出错的约定都能在 node 环境下直接测。
 *
 * 后端对应实现：`app/api/auth.py`。
 */

import { apiFetch, readErrorDetail } from './api';

/** 当前登录用户。字段与后端 `UserRead` 对齐（snake_case 是线上契约）。 */
export interface AuthUser {
  id: string;
  email: string;
  is_active: boolean;
  is_superuser: boolean;
  is_verified: boolean;
}

export type AuthResult = { ok: true } | { ok: false; message: string };

/**
 * 密码最小长度，与后端 `app/services/auth.py:MIN_PASSWORD_LENGTH` 保持一致。
 *
 * 前端校验只是为了**即时反馈**，不是安全边界——真正的校验必须在服务端，因为
 * 客户端的一切都可以绕过。两边都留着是有意的：前端给体验，后端给保证。
 */
export const MIN_PASSWORD_LENGTH = 8;

/**
 * 取当前登录用户。
 *
 * 未登录返回 `null`：启动时探测登录态本来就会拿到 401，这是**预期结果而非错误**，
 * 所以关掉 401 的全局通知，也不抛异常。
 */
export async function fetchCurrentUser(): Promise<AuthUser | null> {
  const response = await apiFetch('/api/auth/me', { notifyUnauthorized: false });

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
  });

  if (response.ok) return { ok: true };

  return { ok: false, message: describeAuthError(await readErrorDetail(response), response.status) };
}

/** 注册。成功返回 201，但**不会自动登录**——调用方需要再走一次 `login`。 */
export async function register(email: string, password: string): Promise<AuthResult> {
  const response = await apiFetch('/api/auth/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
    notifyUnauthorized: false,
  });

  if (response.ok) return { ok: true };

  return { ok: false, message: describeAuthError(await readErrorDetail(response), response.status) };
}

/** 登出。服务端会删掉会话记录，因此这是**真实失效**，不只是清本地 cookie。 */
export async function logout(): Promise<void> {
  await apiFetch('/api/auth/logout', { method: 'POST', notifyUnauthorized: false });
}

/**
 * 后端错误码 → 用户可读文案。
 *
 * 后端返回的是**错误码**（如 `LOGIN_BAD_CREDENTIALS`），直接显示毫无意义。
 *
 * 未知内容原样透出：宁可让用户看到一个陌生字符串，也不要假装成功、或用一句笼统的
 * 「操作失败」把可诊断的信息吞掉。`reason` 类错误（如密码太短）本身就是中文说明，
 * 会直接落到这里。
 */
const AUTH_ERROR_MESSAGES: Record<string, string> = {
  LOGIN_BAD_CREDENTIALS: '邮箱或密码错误',
  REGISTER_USER_ALREADY_EXISTS: '该邮箱已注册',
  REGISTER_INVALID_PASSWORD: `密码不符合要求（至少 ${MIN_PASSWORD_LENGTH} 个字符）`,
};

export function describeAuthError(detail: string, status?: number): string {
  if (detail) return AUTH_ERROR_MESSAGES[detail] ?? detail;
  return status ? `请求失败（HTTP ${status}）` : '请求失败，请稍后重试';
}
