/**
 * 思考耗时的秒数口径。
 *
 * 直播计时与结算耗时此前两套取整：streaming 用 floor、拿到持久化毫秒数后用
 * round，同一段思考会先显示 4s、结束后跳成 5s。这里统一成 floor + 至少 1s，
 * 数字只会小于等于真实，不会出现「结束后反而变多」。
 */
export function thinkingSeconds(ms: number): number {
  return ms > 0 ? Math.max(1, Math.floor(ms / 1000)) : 0;
}

/**
 * 直播计时的起点。
 *
 * 优先用 run 上记录的 `thinkingStartedAt`：切到别的会话再回来时组件会重挂载，
 * 单看 `elapsedMs`（内容出现前一直是 0）会把计时清零重来。没有起点时退回
 * 「现在 - 已持久化的耗时」，与旧行为一致。
 */
export function thinkingStartTime(
  startedAt: number | null | undefined,
  elapsedMs: number,
  now: number = Date.now(),
): number {
  if (typeof startedAt === 'number' && Number.isFinite(startedAt) && startedAt > 0) {
    return startedAt;
  }
  return now - Math.max(0, elapsedMs);
}
