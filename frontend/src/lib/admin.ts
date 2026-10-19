/**
 * 管理后台接口客户端。
 *
 * 与 `lib/auth.ts` 同样的分工：这里只做「请求 + 归一化 + 错误翻译」，不持有 React
 * 状态。组件层负责加载/刷新/表单草稿；纯函数（`buildProviderPayload`、
 * `describeAdminError`）放在这里是为了能在 node 环境的 vitest 里直接测。
 *
 * 后端对应实现：`app/api/admin.py`。所有接口都要求超级管理员，普通用户会拿到 403。
 */

import { apiFetch, readErrorDetail } from './api';

// ---------------------------------------------------------------------
// 线上契约（snake_case 与后端一致）
// ---------------------------------------------------------------------
export interface AdminProvider {
  id: string;
  name: string;
  slug: string;
  base_url: string;
  api_key_hint: string | null;
  enabled: boolean;
  model_count: number;
  created_at: string;
}

export interface AdminModel {
  id: string;
  provider_id: string;
  /** 对外路由键：`{provider.slug}/{model_id}`，对话请求用它。 */
  key: string;
  model_id: string;
  display_name: string;
  description: string | null;
  reasoning: boolean | null;
  supported_reasoning_levels: string[];
  context_window: number | null;
  max_output_tokens: number | null;
  enabled: boolean;
  is_default: boolean;
}

export interface AdminUser {
  id: string;
  email: string;
  is_active: boolean;
  is_superuser: boolean;
  is_verified: boolean;
}

export interface AdminUserPage {
  items: AdminUser[];
  total: number;
  limit: number;
  offset: number;
}

export interface ProviderSyncResult {
  fetched: number;
  created: number;
  updated: number;
}

export interface ProviderTestResult {
  ok: boolean;
  model_count: number;
  error: string | null;
}

// ---------------------------------------------------------------------
// 请求
// ---------------------------------------------------------------------
export class AdminApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'AdminApiError';
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(path, {
    ...init,
    headers: {
      ...(init?.body ? { 'Content-Type': 'application/json' } : {}),
      ...(init?.headers ?? {}),
    },
  });

  if (!response.ok) {
    // 403 的 detail 是框架给的 "Forbidden"，翻成用户能懂的话；其余沿用后端文案
    const detail = await readErrorDetail(response);
    throw new AdminApiError(describeAdminError({ status: response.status, detail }), response.status);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export interface ProviderPayload {
  name?: string;
  base_url?: string;
  api_key?: string;
  enabled?: boolean;
}

export interface ProviderDraft {
  name: string;
  baseUrl: string;
  /** 创建时为空 = 不配置密钥；编辑时为空 = 保持不变。 */
  apiKey: string;
  enabled: boolean;
  /** 编辑时显式清空已保存的密钥。 */
  clearApiKey: boolean;
}

/**
 * 表单草稿 → 请求体。
 *
 * 关键约定：编辑时**不带** `api_key` 字段表示「不修改」。后端用
 * `exclude_unset` 区分「没传」与「显式传空串」，所以这里不能把空串当成
 * 「清空」偷偷发出去——那会让用户只改个名字就把密钥弄丢。
 */
export function buildProviderPayload(
  draft: ProviderDraft,
  mode: 'create' | 'edit',
): ProviderPayload {
  const payload: ProviderPayload = {
    name: draft.name.trim(),
    base_url: draft.baseUrl.trim(),
    enabled: draft.enabled,
  };

  const apiKey = draft.apiKey.trim();
  if (mode === 'create') {
    if (apiKey) payload.api_key = apiKey;
    return payload;
  }

  if (draft.clearApiKey) {
    payload.api_key = '';
  } else if (apiKey) {
    payload.api_key = apiKey;
  }
  return payload;
}

export function listProviders(): Promise<AdminProvider[]> {
  return request<AdminProvider[]>('/api/admin/providers');
}

export function createProvider(payload: ProviderPayload): Promise<AdminProvider> {
  return request<AdminProvider>('/api/admin/providers', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function updateProvider(id: string, payload: ProviderPayload): Promise<AdminProvider> {
  return request<AdminProvider>(`/api/admin/providers/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
}

export function deleteProvider(id: string): Promise<void> {
  return request<void>(`/api/admin/providers/${id}`, { method: 'DELETE' });
}

export function testProvider(id: string): Promise<ProviderTestResult> {
  return request<ProviderTestResult>(`/api/admin/providers/${id}/test`, { method: 'POST' });
}

export function syncProvider(id: string): Promise<ProviderSyncResult> {
  return request<ProviderSyncResult>(`/api/admin/providers/${id}/sync`, { method: 'POST' });
}

export function listProviderModels(providerId: string): Promise<AdminModel[]> {
  return request<AdminModel[]>(`/api/admin/providers/${providerId}/models`);
}

export interface ModelPayload {
  model_id?: string;
  display_name?: string;
  reasoning?: boolean | null;
  supported_reasoning_levels?: string[];
  context_window?: number | null;
  max_output_tokens?: number | null;
  enabled?: boolean;
  is_default?: boolean;
}

export function createProviderModel(providerId: string, payload: ModelPayload): Promise<AdminModel> {
  return request<AdminModel>(`/api/admin/providers/${providerId}/models`, {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function updateModel(modelId: string, payload: ModelPayload): Promise<AdminModel> {
  return request<AdminModel>(`/api/admin/models/${modelId}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
}

export function deleteModel(modelId: string): Promise<void> {
  return request<void>(`/api/admin/models/${modelId}`, { method: 'DELETE' });
}

export function listUsers(params: {
  query?: string;
  limit?: number;
  offset?: number;
}): Promise<AdminUserPage> {
  const search = new URLSearchParams();
  if (params.query?.trim()) search.set('query', params.query.trim());
  if (params.limit !== undefined) search.set('limit', String(params.limit));
  if (params.offset !== undefined) search.set('offset', String(params.offset));

  const suffix = search.toString() ? `?${search.toString()}` : '';
  return request<AdminUserPage>(`/api/admin/users${suffix}`);
}

export function banUser(userId: string): Promise<AdminUser> {
  return request<AdminUser>(`/api/admin/users/${userId}/ban`, { method: 'POST' });
}

export function unbanUser(userId: string): Promise<AdminUser> {
  return request<AdminUser>(`/api/admin/users/${userId}/unban`, { method: 'POST' });
}

// ---------------------------------------------------------------------
// 展示辅助
// ---------------------------------------------------------------------
/**
 * 把一次失败的请求翻译成可读文案。
 *
 * 只在**没有后端 detail** 时按状态码兜底；有 detail 时优先用它（后端已经写了中文
 * 业务说明，例如「停用的模型不能设为默认模型」）。
 */
export function describeAdminError(error: { status?: number; detail?: string } | unknown): string {
  const status = typeof error === 'object' && error !== null ? (error as { status?: number }).status : undefined;
  const detail =
    typeof error === 'object' && error !== null ? (error as { detail?: string }).detail : undefined;

  if (detail && !/^forbidden$/i.test(detail.trim())) return detail;

  switch (status) {
    case 401:
      return '登录已失效，请重新登录';
    case 403:
      return '需要超级管理员权限';
    case 404:
      return '目标不存在，可能已被其他管理员删除';
    default:
      return '操作失败，请稍后重试';
  }
}

/**
 * 组件层的统一错误文案。
 *
 * `AdminApiError.message` 已经过 `describeAdminError` 翻译（后端的中文 detail 被原样
 * 保留），直接用它；网络故障没有状态码也没有 detail，走兜底文案——不要把
 * "Failed to fetch" 这种英文底层异常甩给用户。
 */
export function describeFailure(error: unknown): string {
  if (error instanceof AdminApiError) return error.message;
  return describeAdminError(error);
}

export function formatContextWindow(tokens: number | null): string {
  if (!tokens || tokens <= 0) return '窗口未知';
  if (tokens >= 1_000_000) return `${Math.round(tokens / 100_000) / 10}M 上下文`;
  if (tokens >= 1_000) return `${Math.round(tokens / 1_000)}K 上下文`;
  return `${tokens} 上下文`;
}
