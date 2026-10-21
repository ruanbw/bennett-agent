/**
 * 把「请求为什么没完成」翻译成用户能照着行动的中文，并判断值不值得给重试按钮。
 *
 * 之前对话流的异常直接拼 `err.message`：Chrome 是「Failed to fetch」、Safari 是
 * 「Load failed」；HTTP 失败则是一句「HTTP 错误: 504」——两种都只告诉用户「坏了」，
 * 不告诉他是网络、服务端还是请求本身，也没给出重试入口。
 *
 * 判定依据优先用结构化字段（`ChatRequestError` 的 status / Retry-After），其次才看
 * 错误名与文案；文案匹配只作为兜底（各浏览器的 fetch 拒绝信息并不统一）。
 */

import { ChatRequestError } from './chat-request-error';

export interface ConnectionFailure {
  /** 面向用户的中文说明。 */
  message: string;
  /** 是否值得显示「重试」：重试会改变结果，给出按钮才有意义。 */
  retryable: boolean;
}

const NETWORK_ERROR_PATTERN =
  /failed to fetch|load failed|networkerror|network error|internet connection appears to be offline/i;
const HTTP_STATUS_PATTERN = /^HTTP\s+(\d{3})$/;

const NETWORK_MESSAGE = '网络连接中断，请检查网络后重试';
const TIMEOUT_MESSAGE = '请求超时，请重试';
const GENERIC_MESSAGE = '请求失败，请稍后重试';

function statusOf(error: unknown): number | null {
  if (error instanceof ChatRequestError) return error.status;
  if (error instanceof Error) {
    const matched = HTTP_STATUS_PATTERN.exec(error.message.trim());
    if (matched) {
      const parsed = Number.parseInt(matched[1], 10);
      if (Number.isFinite(parsed)) return parsed;
    }
  }
  return null;
}

function isTimeout(error: unknown): boolean {
  const name = (error as { name?: unknown } | null)?.name;
  return name === 'AbortError' || name === 'TimeoutError';
}

function describeStatus(status: number, error: unknown): ConnectionFailure {
  const detail = error instanceof ChatRequestError ? error.detail : null;

  if (status === 429) {
    // 对话接口把「等几秒」写进了 detail；Retry-After 解析不出时退回「稍后」
    return { message: detail ?? '请求过于频繁，请稍后重试', retryable: true };
  }
  if (status === 401) return { message: '登录状态已失效，请重新登录', retryable: false };
  if (status >= 500) {
    // 上游/网关的错误细节通常不可行动，甚至可能带内部信息；重试才是用户能做的事
    return { message: `服务器暂时不可用（HTTP ${status}），请稍后重试`, retryable: true };
  }
  // 4xx 是请求本身的问题：重试同一个请求结果不会变，详情直接给用户
  return { message: detail ?? `请求未能完成（HTTP ${status}）`, retryable: false };
}

export function describeConnectionFailure(error: unknown): ConnectionFailure {
  const status = statusOf(error);
  if (status !== null) return describeStatus(status, error);

  if (isTimeout(error)) return { message: TIMEOUT_MESSAGE, retryable: true };

  if (error instanceof TypeError || (error instanceof Error && NETWORK_ERROR_PATTERN.test(error.message))) {
    return { message: NETWORK_MESSAGE, retryable: true };
  }

  // 我们自己写的、已经带中文说明的错误（例如「响应没有可读的流式主体」）优先透传；
  // 透传前加「请求失败」让用户知道这是这一轮没完成，而不是回答内容的一部分。
  // 已经自带这个前缀的不再叠加——消息区本来就会写「请求失败: 」，两层前缀会变成
  // 「请求失败: 请求失败：…」。
  if (error instanceof Error && /[\u4e00-\u9fff]/.test(error.message)) {
    const message = error.message.trim();
    if (message.startsWith('请求失败')) return { message, retryable: true };
    return { message: `请求失败：${message}`, retryable: true };
  }

  return { message: GENERIC_MESSAGE, retryable: true };
}
