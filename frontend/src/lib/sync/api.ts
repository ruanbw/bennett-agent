/**
 * 同步接口的 HTTP 客户端。只做「请求 + 归一化错误」，不含任何调度逻辑（那是 engine 的事）。
 *
 * 错误分三类，因为客户端的反应完全不同：
 *
 *   - `invalid_cursor` / `410`：游标不可用 → 丢弃游标、全量重拉；
 *   - 其它 4xx：请求本身不合法（协议 bug / 超限）→ 停下来报告，重试没有意义；
 *   - 5xx / 网络错误：可重试 → 退避。
 */

import { apiFetch, readErrorDetail } from '../api';
import { parseRetryAfterSeconds } from '../retry-after';
import type { ConversationPushRow, ConversationRow, MessagePushRow, MessageRow } from './mapping';

export interface PushRejection {
  kind: 'conversation' | 'message';
  id: string;
  reason: string;
}

export interface PushResponse {
  version: number;
  accepted: { conversations: number; messages: number };
  rejected: PushRejection[];
}

export interface PullResponse {
  cursor: string;
  has_more: boolean;
  conversations: ConversationRow[];
  messages: MessageRow[];
}

export interface PushBody {
  conversations: ConversationPushRow[];
  messages: MessagePushRow[];
}

/**
 * push 的超时：批量最大 200 行、每行 payload 上限 256 KB，正常几秒内完成。
 * 20s 是为了不把「服务端在忙」误判成断网。
 */
const PUSH_TIMEOUT_MS = 20_000;

/**
 * pull 的超时要比服务端长轮询上限（默认 25s）更长，否则我们会在服务端正要返回时
 * 主动掐断，表现为「每次长轮询都被当成网络错误」。
 */
export const PULL_TIMEOUT_MS = 40_000;

export class SyncHttpError extends Error {
  readonly status: number;
  /** 服务端在 detail 末尾附带的机器可读 code（如 `invalid_cursor`）。 */
  readonly code: string | null;
  /** 429 时服务端要求等待的秒数；其它响应或没有该头时为 null。 */
  readonly retryAfterSeconds: number | null;

  constructor(
    message: string,
    status: number,
    code: string | null,
    retryAfterSeconds: number | null = null,
  ) {
    super(message);
    this.name = 'SyncHttpError';
    this.status = status;
    this.code = code;
    this.retryAfterSeconds = retryAfterSeconds;
  }

  /** 游标失效：客户端应丢弃游标做一次全量拉取。 */
  get isCursorReset(): boolean {
    return this.status === 410 || this.code === 'invalid_cursor';
  }

  /**
   * 4xx 且不是游标/限流问题：请求本身有问题，重试不会成功。
   *
   * 429 必须排除：限流是暂时状态，服务端还会在 `Retry-After` 里给出恢复时间。
   * 把它当 fatal 等于一次限流就让同步永久停下，只能等用户手动点重试。
   */
  get isFatal(): boolean {
    return this.status >= 400 && this.status < 500 && this.status !== 429 && !this.isCursorReset;
  }
}

/** 后端把机器码写在 detail 末尾：`……（code=invalid_cursor）`。 */
function extractCode(detail: string): string | null {
  const match = /code=([a-z_]+)/.exec(detail);
  return match ? match[1] : null;
}

async function toError(response: Response): Promise<SyncHttpError> {
  const detail = await readErrorDetail(response).catch(() => '');
  const message = detail || `同步请求失败（HTTP ${response.status}）`;
  return new SyncHttpError(
    message,
    response.status,
    extractCode(detail),
    parseRetryAfterSeconds(response.headers.get('Retry-After')),
  );
}

export async function pushRows(body: PushBody): Promise<PushResponse> {
  const response = await apiFetch('/api/sync/push', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
    timeoutMs: PUSH_TIMEOUT_MS,
  });
  if (!response.ok) throw await toError(response);
  return (await response.json()) as PushResponse;
}

export async function pullRows(
  cursor: string | null,
  options: { timeoutSeconds?: number; limit?: number } = {},
): Promise<PullResponse> {
  const params = new URLSearchParams();
  if (cursor) params.set('cursor', cursor);
  if (options.timeoutSeconds !== undefined) params.set('timeout', String(options.timeoutSeconds));
  if (options.limit !== undefined) params.set('limit', String(options.limit));

  const query = params.toString();
  const response = await apiFetch(`/api/sync/pull${query ? `?${query}` : ''}`, {
    timeoutMs: PULL_TIMEOUT_MS,
  });
  if (!response.ok) throw await toError(response);
  return (await response.json()) as PullResponse;
}
