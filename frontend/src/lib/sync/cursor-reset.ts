/**
 * 游标反复失效的守卫。
 *
 * 正常路径：服务端说游标无效（回滚过库、换了数据目录）→ 客户端丢掉游标做一次全量重拉，
 * 之后一切照旧。
 *
 * 但如果**每次**全量重拉之后游标还是不认（服务端版本被回滚、两份数据对不上、某个边界下
 * head 与游标互相矛盾），这条路径就变成一个死循环：拉 → 410 → 全量拉 → 推 → 拉 → 410 …
 * 实测能在一秒内打出几十个请求，最终把自己的账号打成 429，服务器也在做无用功。
 *
 * 所以这里把「连续失效」计数化：第一次按原逻辑处理（丢游标 + 标脏让本机数据补推上去），
 * 之后的重拉不再重复标脏（标脏会把整库重新推一遍，是这场风暴的主要放大器），超过阈值就
 * 停下来报一条可读的错误，交给用户手动「立即同步」。
 */

/** 连续多少次游标失效后停止自动重试。 */
export const MAX_CURSOR_RESETS = 3;

/** 重拉之间的停顿：即使只有一次失效，也不该立刻把下一轮请求打出去。 */
export const CURSOR_RESET_DELAY_MS = 1_000;

export interface CursorResetDecision {
  /** `resync`：丢掉游标、全量重拉；`giveup`：停止自动重试并报错。 */
  action: 'resync' | 'giveup';
  /** 是否要把本机数据全部标脏（只有一段失效里的第一次需要）。 */
  markAllPending: boolean;
  /** 这是连续第几次失效（从 1 开始），用于日志与错误文案。 */
  attempt: number;
}

/** 停下来的原因，单独一个类型便于 `describeSyncFailure` 给出可读文案。 */
export class SyncCursorLoopError extends Error {
  readonly attempts: number;

  constructor(attempts: number) {
    super(`同步游标连续 ${attempts} 次失效，已停止自动重试`);
    this.name = 'SyncCursorLoopError';
    this.attempts = attempts;
  }
}

export interface CursorResetGuard {
  /** 记一次失效并给出处置方式。 */
  record(): CursorResetDecision;
  /** 一次成功的拉取之后调用：计数清零。 */
  reset(): void;
  /** 当前连续失效次数。 */
  attempts(): number;
}

export function createCursorResetGuard(max: number = MAX_CURSOR_RESETS): CursorResetGuard {
  let attempts = 0;
  return {
    record(): CursorResetDecision {
      attempts += 1;
      if (attempts > max) return { action: 'giveup', markAllPending: false, attempt: attempts };
      return { action: 'resync', markAllPending: attempts === 1, attempt: attempts };
    },
    reset(): void {
      attempts = 0;
    },
    attempts(): number {
      return attempts;
    },
  };
}
