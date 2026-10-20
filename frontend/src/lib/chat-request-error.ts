/**
 * 非 2xx 的对话请求错误。
 *
 * 单独成模块（而不是放在 `chat-transport` 里）：错误分类模块要用它判断
 * 「值不值得给重试按钮」，而 `chat-transport` 在单测里常被整体 mock 掉；把类放在
 * 这里，mock 传输层时分类逻辑仍然拿得到真实的构造器。
 */

export class ChatRequestError extends Error {
  readonly status: number;
  /** 后端给的可外泄细节；响应体不是 JSON 时为 null。 */
  readonly detail: string | null;
  /** 429 时 `Retry-After` 的秒数；没有/解析不出为 null。 */
  readonly retryAfterSeconds: number | null;

  constructor(status: number, detail: string | null = null, retryAfterSeconds: number | null = null) {
    super(detail ? `HTTP ${status}: ${detail}` : `HTTP 错误: ${status}`);
    this.name = 'ChatRequestError';
    this.status = status;
    this.detail = detail;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}
