/**
 * 计时明细的口径与容错读取。
 *
 * 服务端在 `usage` 事件里带一个 `timing` 块（见 `app/schemas/events.py`），前端用它
 * 显示「1 轮 3 步 · 208 tok/s」「首 token 平均 420ms」这类读数。
 *
 * ## 为什么整块给或整块缺席，而不是填 0
 *
 * 「0 tok/s」会被读成模型卡住了，「首字 0ms」会被读成秒回——两者都是**错误结论**，
 * 而真实情况只是「这一项没量到」。所以缺任何一个必需字段就当整块没有，界面少显示
 * 一个药丸，而不是显示一个假的数。
 *
 * ## 为什么速度的分子分母必须都是解码阶段的
 *
 * 一轮里有工具调用就有多次模型请求，中间还夹着工具执行与排队。拿整轮墙钟当分母，
 * 速度会被工具耗时稀释成「模型很慢」，而实际上是工具慢。只有 `decodeTokens /
 * decodeMs` 才回答「模型吐字有多快」。
 */

import type { UsageTiming } from '../types';

const positive = (value: unknown): number | null =>
  typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;

/**
 * 从 `metadata.custom.usage.timing`（运行时形状，camelCase）读出计时明细。
 *
 * 必需：`steps` / `turnMs`（缺一个就整块视为没有——没有步数的「轮次统计」没有意义）。
 * 可选：`llmMs` / `toolMs` 缺失按 0 处理（它们只是明细里的两行，不影响药丸本体）；
 * `ttftMs` / `decodeMs` / `decodeTokens` 缺失保持 null，对应读数隐藏。
 */
export function readUsageTiming(value: unknown): UsageTiming | null {
  if (!value || typeof value !== 'object') return null;
  const {
    steps,
    turnMs,
    llmMs,
    toolMs,
    ttftMs,
    decodeMs,
    decodeTokens,
  } = value as Partial<UsageTiming>;

  const safeSteps = positive(steps);
  const safeTurnMs = positive(turnMs);
  if (safeSteps === null || safeTurnMs === null) return null;

  const safeDecodeMs = positive(decodeMs);
  const safeDecodeTokens = positive(decodeTokens);

  return {
    steps: Math.round(safeSteps),
    turnMs: Math.round(safeTurnMs),
    llmMs: Math.round(positive(llmMs) ?? 0),
    toolMs: Math.round(positive(toolMs) ?? 0),
    ttftMs: positive(ttftMs),
    // 分子分母成对：只给一个就算不出速度，宁可整项没有
    decodeMs: safeDecodeMs,
    decodeTokens: safeDecodeTokens,
  };
}

/**
 * 线上事件（snake_case）→ 运行时形状（camelCase）。
 *
 * **只在这里转一次**，之后整条前端链路都用 camelCase。理由与 `contextTokens` 那一组
 * 字段相同：线上契约是 snake_case，本地形状是 camelCase，两者在 store 的边界对齐；
 * 若让「读的时候兼容两种命名」散在消费端，每加一个字段就要在每处读取点各兼容一遍，
 * 漏掉一处就是**静默返回 null**——药丸不出现，而没有任何报错指向真正的原因。
 *
 * 校验交给 `readUsageTiming`：这里只负责改名，不负责判断有效性。
 */
export function usageTimingFromWire(value: unknown): UsageTiming | null {
  if (!value || typeof value !== 'object') return null;
  const wire = value as Record<string, unknown>;

  return readUsageTiming({
    steps: wire.steps,
    turnMs: wire.turn_ms,
    llmMs: wire.llm_ms,
    toolMs: wire.tool_ms,
    ttftMs: wire.ttft_ms,
    decodeMs: wire.decode_ms,
    decodeTokens: wire.decode_tokens,
  });
}

/**
 * 输出速度（tok/s）；量不到时返回 null。
 *
 * 这里只做除法，不做格式化——格式化在 `session-stats.ts`，那才是纯展示逻辑。
 */
export function decodeTokensPerSecond(timing: UsageTiming | null | undefined): number | null {
  if (!timing) return null;
  const { decodeMs, decodeTokens } = timing;
  if (decodeMs === null || decodeTokens === null || decodeMs <= 0) return null;
  const tps = decodeTokens / (decodeMs / 1000);
  return Number.isFinite(tps) && tps > 0 ? tps : null;
}

/**
 * 步均首 token 延迟；量不到时返回 null。
 *
 * `ttftMs` 在服务端已经是各步平均值，直接返回即可。这层薄封装的意义是把
 * 「字段可能缺失」这条契约收在一处，调用方不必到处写 `?? null`。
 */
export function averageFirstTokenMs(timing: UsageTiming | null | undefined): number | null {
  if (!timing) return null;
  return timing.ttftMs;
}
