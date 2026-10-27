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
import { firstIssue, modelDraftSchema, providerDraftSchema } from './schemas';

export { MAX_MODEL_DISPLAY_NAME_CHARS } from './schemas';

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
  /** 认证方式：API Key 放进哪个请求头（bearer / x-api-key / x-goog-api-key / none）。 */
  auth_style: string;
  /** 创建时使用的内置预设 key；自定义添加为 null。 */
  preset_key: string | null;
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
  /** 最近一次同步的上游原始条目；手动添加或从未同步过为 null。 */
  raw: Record<string, unknown> | null;
}

/** 内置厂商预设（`GET /api/admin/provider-presets`）。 */
export interface ProviderPreset {
  key: string;
  name: string;
  base_url: string;
  auth_style: string;
  api_key_url: string;
  needs_api_key: boolean;
  note: string;
  /** 选择界面的分组：overseas=海外厂商 / cn=国内厂商 / local=本机网关。 */
  group: string;
}

export interface AdminUser {
  id: string;
  email: string;
  is_active: boolean;
  is_superuser: boolean;
  is_verified: boolean;
  /** 角色：`user` / `admin` / `superadmin`。展示徒标与按钮可用性靠它。 */
  role: string;
}

/**
 * 可以通过管理端接口设置的角色。
 *
 * `superadmin` 刻意不在里面：最高一档只能经运维通道（`scripts/seed_admin.py`）授予，
 * 不能通过 HTTP 互相提权。后端 `RoleUpdate` 里的 `Literal` 与这个类型必须一致。
 */
export type SettableRole = 'user' | 'admin';

export const ROLE_LABELS: Record<string, string> = {
  user: '普通用户',
  admin: '管理员',
  superadmin: '超级管理员',
};

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
  /** 仅创建时有意义：从内置厂商预设一键创建，后端据此带入认证方式与固定头。 */
  preset_key?: string;
}

/** 草稿里表示「不使用预设、手动填写地址」的哨兵值。 */
export const CUSTOM_PRESET_KEY = 'custom';

export interface ProviderDraft {
  name: string;
  baseUrl: string;
  /**
   * 选中的预设 key；`custom` 表示手动填写地址。空串只在新增表单里出现，
   * 是「厂商下拉还没选」的占位，语义与 `custom` 相同（校验与请求体都按无预设处理）。
   */
  presetKey: string;
  /** 创建时为空 = 不配置密钥；编辑时为空 = 保持不变。 */
  apiKey: string;
  enabled: boolean;
  /** 编辑时显式清空已保存的密钥。 */
  clearApiKey: boolean;
}

export type ProviderDraftField = 'name' | 'baseUrl' | 'apiKey';

export interface ProviderDraftIssue {
  field: ProviderDraftField;
  message: string;
}

/** 是否走厂商预设：`custom` 与「还没选」的空串都算自定义地址。 */
export function isPresetKey(presetKey: string): boolean {
  return presetKey !== CUSTOM_PRESET_KEY && presetKey !== '';
}

/**
 * 表单草稿的本地校验（schema 见 `lib/schemas.ts`）。
 *
 * 只拦「一定会在服务端失败」的输入，错误文案直接复用后端那句中文：服务端 422 的
 * detail 形如 `body.base_url: Value error, 必须是以 http:// …`，带着英文前缀和字段
 * 路径，对管理员没有帮助。
 */
export function validateProviderDraft(draft: ProviderDraft): ProviderDraftIssue | null {
  const issue = firstIssue(providerDraftSchema, draft);
  return issue ? { field: issue.field as ProviderDraftField, message: issue.message } : null;
}

export function buildProviderPayload(
  draft: ProviderDraft,
  mode: 'create' | 'edit',
): ProviderPayload {
  const payload: ProviderPayload = {
    name: draft.name.trim(),
    enabled: draft.enabled,
  };

  const apiKey = draft.apiKey.trim();
  if (mode === 'create') {
    const baseUrl = draft.baseUrl.trim();
    if (baseUrl) payload.base_url = baseUrl;
    if (isPresetKey(draft.presetKey)) payload.preset_key = draft.presetKey;
    if (apiKey) payload.api_key = apiKey;
    return payload;
  }

  payload.base_url = draft.baseUrl.trim();
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

export function listProviderPresets(): Promise<ProviderPreset[]> {
  return request<ProviderPreset[]>('/api/admin/provider-presets');
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

export type ModelDraftField = 'displayName' | 'contextWindow' | 'maxOutputTokens';

export interface ModelDraft {
  displayName: string;
  /** 空串 = 清空（null）；否则必须是正整数。 */
  contextWindow: string;
  maxOutputTokens: string;
  reasoning: boolean;
}

export interface ModelDraftIssue {
  field: ModelDraftField;
  message: string;
}

function parseOptionalTokenLimit(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  if (!/^\d+$/.test(trimmed)) return null;
  const parsed = Number(trimmed);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

/**
 * 编辑模型元数据的本地校验。
 *
 * 名称是必填；两个 token 上限可留空表示「清空/未知」，但只要填了就必须是正整数。
 * 后端对非法数字返回的是 Pydantic 的英文 422，对管理员没有帮助，所以先在这里拦。
 */
/**
 * 编辑模型元数据的本地校验（zod schema 见 `lib/schemas.ts`）。
 *
 * 后端对非法数字返回的是 Pydantic 的英文 422，对管理员没有帮助，所以先在这里拦。
 */
export function validateModelDraft(draft: ModelDraft): ModelDraftIssue | null {
  const issue = firstIssue(modelDraftSchema, draft);
  return issue ? { field: issue.field as ModelDraftField, message: issue.message } : null;
}

export function buildModelPayload(draft: ModelDraft): ModelPayload {
  return {
    display_name: draft.displayName.trim(),
    context_window: parseOptionalTokenLimit(draft.contextWindow),
    max_output_tokens: parseOptionalTokenLimit(draft.maxOutputTokens),
    reasoning: draft.reasoning,
  };
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

/** 用户列表的状态筛选：`all` 不传参数（后端默认也是 all）。 */
export type UserStatusFilter = 'all' | 'active' | 'banned';

export function listUsers(params: {
  query?: string;
  status?: UserStatusFilter;
  limit?: number;
  offset?: number;
}): Promise<AdminUserPage> {
  const search = new URLSearchParams();
  if (params.query?.trim()) search.set('query', params.query.trim());
  if (params.status && params.status !== 'all') search.set('status', params.status);
  if (params.limit !== undefined) search.set('limit', String(params.limit));
  if (params.offset !== undefined) search.set('offset', String(params.offset));

  const suffix = search.toString() ? `?${search.toString()}` : '';
  return request<AdminUserPage>(`/api/admin/users${suffix}`);
}

/**
 * 把页码偏移夹进「按当前总数仍存在的最后一页」。
 *
 * 典型场景：第 2 页只剩 1 个用户，管理员把他封禁后总数从 21 变成 20——原 offset=20
 * 已经越界，不夹回去就会停在一个永远空着的第 2 页，看起来像「用户被删光了」。
 * 纯函数便于在 node 环境测试；limit <= 0 属于调用方 bug，按第 0 页处理。
 */
export function clampUsersOffset(offset: number, limit: number, total: number): number {
  if (limit <= 0 || total <= 0) return 0;
  const lastOffset = (Math.ceil(total / limit) - 1) * limit;
  return Math.max(0, Math.min(offset, lastOffset));
}

export function banUser(userId: string): Promise<AdminUser> {
  return request<AdminUser>(`/api/admin/users/${userId}/ban`, { method: 'POST' });
}

export function unbanUser(userId: string): Promise<AdminUser> {
  return request<AdminUser>(`/api/admin/users/${userId}/unban`, { method: 'POST' });
}

/**
 * 设置用户角色（仅 superadmin）。
 *
 * 成功后后端会删掉该用户的全部会话，因此对方下一次请求就是 401，需要重新登录。
 */
export function setUserRole(userId: string, role: SettableRole): Promise<AdminUser> {
  return request<AdminUser>(`/api/admin/users/${userId}/role`, {
    method: 'PATCH',
    body: JSON.stringify({ role }),
  });
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
      return '权限不足（需要管理员或超级管理员）';
    case 404:
      return '目标不存在，可能已被其他管理员删除';
    default:
      return '操作失败，请稍后重试';
  }
}

/**
 * 组件层的统一错误文案。
 *
 * `AdminApiError.message` 已经过 `describeAdminError` 翻译，直接用它；网络故障没有
 * 状态码也没有 detail，走兜底文案——不要把 "Failed to fetch" 这种英文甩给用户。
 */
export function describeFailure(error: unknown): string {
  if (error instanceof AdminApiError) return error.message;
  return describeAdminError(error);
}

/**
 * 同步结果的用户文案。
 *
 * 「新增 0 / 更新 0」对管理员没有信息量：他刚点完同步，需要知道上游到底给了多少、
 * 自己手动加的模型会不会被删。后端的策略是**只增不删**，这句必须写出来，否则
 * 「上游没返回它」会被理解成「它被删了」。
 */
export function describeProviderSync(result: ProviderSyncResult): string {
  if (result.fetched === 0) {
    return '同步完成：上游未返回任何模型；已保存的模型不会被删除。';
  }

  const parts = [`上游 ${result.fetched} 个`];
  if (result.created > 0) parts.push(`新增 ${result.created}`);
  if (result.updated > 0) parts.push(`更新 ${result.updated}`);
  if (result.created === 0 && result.updated === 0) parts.push('没有变化');

  return `同步完成：${parts.join('，')}；上游未返回的模型不会自动删除。`;
}

/** 模型列表默认平铺多少个；超出部分收进「展开全部」。 */
export const VISIBLE_MODELS_COLLAPSED = 8;

/**
 * 按关键词过滤模型（`model_id` 与展示名都匹配，大小写不敏感）。
 *
 * 一个 LiteLLM 网关常常暴露几十上百个模型，后台把它们全部平铺时，找某个具体型号只能靠
 * 滚动与肉眼扫描——而管理动作（启停、设默认、编辑）恰恰都是「针对某一个模型」的。
 *
 * 空查询返回原数组（保持调用方的引用，避免无谓重渲染）。
 */
export function filterAdminModels<T extends { model_id: string; display_name: string }>(
  models: readonly T[],
  query: string,
): readonly T[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return models;
  return models.filter(
    (model) =>
      model.model_id.toLowerCase().includes(needle) ||
      model.display_name.toLowerCase().includes(needle),
  );
}

export function formatContextWindow(tokens: number | null): string {
  if (!tokens || tokens <= 0) return '窗口未知';
  if (tokens >= 1_000_000) return `${Math.round(tokens / 100_000) / 10}M 上下文`;
  if (tokens >= 1_000) return `${Math.round(tokens / 1_000)}K 上下文`;
  return `${tokens} 上下文`;
}
