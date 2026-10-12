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
