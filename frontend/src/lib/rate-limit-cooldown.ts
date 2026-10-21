/**
 * 429 的「还要等多久」在界面上的口径。
 *
 * 后端在 `Retry-After` 里给了秒数，对话请求把它换算成**绝对时刻**存进 `run.error`
 * （`retryAfterAt`）。此前这个值拿到就丢了：界面上的「重试」一直可点，用户点一次、
 * 再吃一次 429，而提示里的「请 N 秒后重试」已经滚过去了。
 *
 * 这里只做两件事，都是纯函数：算剩余时间（带边界防护）、把它变成一句倒计时文案。
 */

/** 冷却上限：服务端给一个荒谬的大值（或数据被写坏）时不能把按钮永久禁用。 */
export const MAX_COOLDOWN_MS = 60 * 60 * 1000;

/**
 * 距离可以重试还有多久（毫秒）。没有可用的 `retryAfterAt` 时返回 0（不冷却）。
 *
 * 校验输入类型：这个值来自落库的 metadata，可能是老记录、也可能被改坏。
 */
export function remainingCooldownMs(retryAfterAt: unknown, now: number = Date.now()): number {
  if (typeof retryAfterAt !== 'number' || !Number.isFinite(retryAfterAt)) return 0;
  const remaining = retryAfterAt - now;
  if (remaining <= 0) return 0;
  return Math.min(remaining, MAX_COOLDOWN_MS);
}

/**
 * 冷却中的按钮文案：`12 秒后可重试`。剩余为 0（不需要冷却）时返回空串。
 *
 * 向上取整：剩 0.4 秒时显示「1 秒后」而不是「0 秒后」——后者会让用户以为立即可点，
 * 点了又失败一次。
 */
export function describeCooldown(remainingMs: number): string {
  if (!Number.isFinite(remainingMs) || remainingMs <= 0) return '';
  return `${Math.ceil(remainingMs / 1000)} 秒后可重试`;
}
