/**
 * 长轮询超时的判定策略。
 *
 * ## 为什么超时不能直接当失败
 *
 * pull 是 25 秒的长轮询，客户端给 40 秒预算。请求超过 40 秒通常不是「服务器挂了」，
 * 而是浏览器把标签页冻结/节流了（定时器在冻结期间不跑，恢复时立刻到期），或者网络栈
 * 卡了一下。把它当成失败会**误报**：状态面板变成「同步失败」，用户以为数据没保存，
 * 而实际上一切正常、下一次轮询就成功了。
 *
 * 真实故障的特征不一样：断网/服务端不可达会很快抛 `TypeError`（fetch failed）或拿到
 * 5xx，不会等到 40 秒。所以只有**连续**多次超时才值得报告——单次超时静默重试，
 * 连续 `MAX_SILENT_PULL_TIMEOUTS` 次才当成真失败。
 *
 * 首次失败期间状态保持上一次成功时的样子（「已同步 + 上次同步时间」），这比一句没有
 * 下文的「同步失败」更接近事实。
 */

/** 连续多少次长轮询超时才上报为失败。 */
export const MAX_SILENT_PULL_TIMEOUTS = 3;

/** `AbortSignal.timeout()` 触发时抛出的错误：name 是 TimeoutError。 */
export function isPullTimeout(err: unknown): boolean {
  return err instanceof Error && err.name === 'TimeoutError';
}

export interface TimeoutStreak {
  /** 记一次超时；返回是否该把它当成真正的失败报给用户。 */
  record(): boolean;
  /** 成功一次就清零（下一次超时重新按「偶发」处理）。 */
  reset(): void;
}

export function createTimeoutStreak(threshold: number = MAX_SILENT_PULL_TIMEOUTS): TimeoutStreak {
  let count = 0;
  return {
    record(): boolean {
      count += 1;
      return count >= threshold;
    },
    reset(): void {
      count = 0;
    },
  };
}
