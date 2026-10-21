/**
 * `Retry-After` 的秒数解析。
 *
 * 服务端（认证 / 对话 / 同步限流）都用 `too_many()` 发同一个头；客户端有三处要读它。
 * 抽出一份，避免「429 各自解析」出现有的等 17 秒、有的盲目重试。
 *
 * 只接受秒数形式：HTTP-date 形式在这个自建后端不会出现，网关塞进来的日期解析不出
 * 就返回 null，让调用方退回普通退避——宁可按自己的节奏重试，也不要解析出一个错的等待时间。
 */
export function parseRetryAfterSeconds(header: string | null): number | null {
  if (header === null) return null;
  const seconds = Number.parseInt(header.trim(), 10);
  return Number.isFinite(seconds) && seconds > 0 ? seconds : null;
}

/**
 * 下一次重试等多久。
 *
 * 服务端明确给了 `Retry-After` 就听它的（只做上限保护），没有才退回客户端退避。
 * 不能取「max(退避, Retry-After)」：退避爬到 60s 后，服务端说 2s 后恢复也会白等一分钟。
 */
export function retryDelayMs(
  retryAfterSeconds: number | null,
  fallbackMs: number,
  maxMs: number,
): number {
  if (retryAfterSeconds === null) return fallbackMs;
  return Math.min(retryAfterSeconds * 1000, maxMs);
}
