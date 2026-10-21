/**
 * 运行状态 store（R-4 拆分之二 + O-1）。
 *
 * 变化理由：内存状态形状与订阅粒度。
 *
 * O-1：改动前只有一个全局 `version` 计数器，任何会话的任何增量都会让所有订阅者
 * 失效。后果是 Sidebar 必须靠「把 running id 拼成字符串当快照」自保，而
 * ChatProvider 在**别的会话**流式输出时也会重渲染。
 *
 * 现在分两类订阅：
 *   - `subscribeRun(sessionId, listener)`：只关心某一个会话的增量；
 *   - `subscribeRunningSessions(listener)`：只关心「哪些会话在跑」，且仅在该集合
 *     真正变化时通知——流式增量不会让侧边栏重渲染。
 *
 * 通知按宏任务合并（`NOTIFY_COALESCE_MS`）：一次网络分片里的多个增量只渲染一次。
 *
 * 已知限制：多标签页打开同一个会话时没有跨页互斥，两边各自持有内存 run、各自落库，
 * 后写的覆盖先写的（IndexedDB 没有跨页事务协调）。本轮不做跨页互斥；真要做，
 * 最小方案是 BroadcastChannel 广播 run 失效通知，让非活跃标签页放弃内存态。
 */

import { NOTIFY_COALESCE_MS } from './constants';
import {
  TRANSPORT_INCOMPLETE,
  type ChatEvent,
  type ErrorCode,
  type PartKind,
  type Run,
  type RunError,
  type ToolCallRecord,
  type ToolOutcome,
} from '../types';

type Listener = () => void;

/**
 * 服务端事件是外部输入：失败分类只接受本版认识的取值。
 *
 * 新版服务端加了新分类而前端还没发版时，不能把一个协议外的字符串带进 run（它会进
 * 落库、进 metadata，然后被渲染层当判据）。认不出就是「未分类」。
 */
function readErrorCode(value: unknown): ErrorCode | null {
  return value === 'context_overflow' ? value : null;
}

/**
 * store 内部的 run 形状：比共享契约里的 `Run` 多几个只在本模块推导/消费的终止标记。
 *
 *   sawError     收到过 `error`——它是契约里的合法终止，不代表内容不完整
 *   damaged      收到过 `transport_incomplete` 且 damaged=true（丢过无法解析的帧）
 *   persistFailed 终态落库失败：DB 里的内容已不可信，内存态必须留着
 */
export interface RunState extends Run {
  sawError: boolean;
  damaged: boolean;
  persistFailed: boolean;
}

const runs = new Map<string, RunState>();
const runListeners = new Map<string, Set<Listener>>();
const runningListeners = new Set<Listener>();
const runVersions = new Map<string, number>();

let runningKey = '';
let flushTimer: ReturnType<typeof setTimeout> | null = null;
const dirtySessions = new Set<string>();

/**
 * 版本号的唯一来源，只增不减。
 *
 * 此前是「按会话自增」：删除会话条目时连版本记录一起删，重建后从 1 重新开始，
 * 于是可能出现「删除 run 后 flush 又把版本写回同一个值」——`useSyncExternalStore`
 * 发现快照没变就不会重渲染，界面停留在已被删除的 run 上。换成全局计数器后，
 * 同一个会话拿到的新版本永远大于它见过的任何旧版本（即使中途删过条目）。
 */
let versionSeed = 0;

// ---------------------------------------------------------------------
// 订阅
// ---------------------------------------------------------------------
export function subscribeRun(sessionId: string, listener: Listener): () => void {
  let listeners = runListeners.get(sessionId);
  if (!listeners) {
    listeners = new Set();
    runListeners.set(sessionId, listeners);
  }
  listeners.add(listener);

  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) runListeners.delete(sessionId);
  };
}

export function subscribeRunningSessions(listener: Listener): () => void {
  runningListeners.add(listener);
  return () => {
    runningListeners.delete(listener);
  };
}

/** 每次该会话的 run 状态或增量变化都会自增，配合 useSyncExternalStore 触发重渲染。 */
export function getRunVersion(sessionId: string): number {
  return runVersions.get(sessionId) ?? 0;
}

/** 仍在生成中的会话 id 拼成的字符串。集合不变时字符串不变，快照因此稳定。 */
export function getRunningSessionsKey(): string {
  return getRunningSessionIds().join(',');
}

/** 仍在生成中的会话 id（按插入顺序）。只服务于 key 快照，不对外导出。 */
function getRunningSessionIds(): string[] {
  const ids: string[] = [];
  for (const run of runs.values()) {
    if (!run.finished) ids.push(run.sessionId);
  }
  return ids;
}

// ---------------------------------------------------------------------
// 通知调度
// ---------------------------------------------------------------------
function scheduleFlush(): void {
  if (flushTimer !== null) return;
  flushTimer = setTimeout(flush, NOTIFY_COALESCE_MS);
}

function flush(): void {
  flushTimer = null;

  const sessions = [...dirtySessions];
  dirtySessions.clear();

  for (const sessionId of sessions) {
    versionSeed += 1;
    runVersions.set(sessionId, versionSeed);
    for (const listener of runListeners.get(sessionId) ?? []) listener();
  }

  const nextKey = getRunningSessionsKey();
  if (nextKey !== runningKey) {
    runningKey = nextKey;
    for (const listener of runningListeners) listener();
  }
}

/** 标记某会话有变化，并按窗口合并通知。 */
function touch(sessionId: string): void {
  dirtySessions.add(sessionId);
  scheduleFlush();
}

/** 立即清掉待处理通知。仅供测试使用，避免用例之间互相干扰。 */
export function flushNow(): void {
  if (flushTimer !== null) {
    clearTimeout(flushTimer);
    flushTimer = null;
  }
  flush();
}

// ---------------------------------------------------------------------
// 读取
// ---------------------------------------------------------------------
export function getRun(sessionId: string): RunState | null {
  return runs.get(sessionId) ?? null;
}

export function isRunActive(sessionId: string): boolean {
  const run = runs.get(sessionId);
  return Boolean(run && !run.finished);
}

export function activeSessionIds(): string[] {
  return [...runs.keys()];
}

// ---------------------------------------------------------------------
// 生命周期
// ---------------------------------------------------------------------
export interface CreateRunParams {
  sessionId: string;
  assistantMsgId: string;
  model: string | null;
  reasoningEffort?: string | null;
}

/**
 * 建立一次运行。同一会话同时只允许一个运行中的任务，已在运行时返回 null。
 *
 * 顺带清掉同会话里已结束的旧 run——否则新轮次会读到上一轮的残留内容。
 *
 * 注意：若上一轮是 persistFailed（落库失败），这里同样会被新 run 顶掉——内存里
 * 只保留最近一轮。跨轮次抢救失败内容不在本轮范围内（需要 ChatProvider/编排层配合）。
 */
export function createRun({
  sessionId,
  assistantMsgId,
  model,
  reasoningEffort = null,
}: CreateRunParams): RunState | null {
  if (isRunActive(sessionId)) return null;

  const previous = runs.get(sessionId);
  if (previous?.finished) runs.delete(sessionId);

  const run: RunState = {
    sessionId,
    assistantMsgId,
    model,
    reasoningEffort,
    controller: new AbortController(),
    startedAt: Date.now(),
    thinking: '',
    content: '',
    thinkingStartedAt: null,
    thinkingMs: 0,
    toolCalls: [],
    partOrder: [],
    clarification: null,
    fallback: null,
    usage: null,
    error: null,
    sawDone: false,
    sawError: false,
    damaged: false,
    persistFailed: false,
    stopped: false,
    incomplete: false,
    finished: false,
  };

  runs.set(sessionId, run);
  touch(sessionId);
  return run;
}

export function abortRun(sessionId: string): void {
  const run = runs.get(sessionId);
  if (!run || run.finished) return;
  run.controller.abort();
}

/**
 * 终止语义的唯一推导处。
 *
 * `done` 与 `error` 都是契约里的合法终止，只有「既没有 done、也没有 error、也不是
 * 用户主动停止、且没丢过帧」才算不完整。此前只看 done，正常报错的轮次也会被标成
 * 不完整并落库。
 */
export function isRunIncomplete(run: RunState): boolean {
  return run.damaged || (!run.sawDone && !run.stopped && !run.sawError);
}

export function finishRun(sessionId: string, { stopped = false } = {}): RunState | null {
  const run = runs.get(sessionId);
  if (!run) return null;

  // 思考计时的收尾：正文出现过就已在 applyEvent 里结算，这里兜住「只有思考没有正文」
  if (run.thinkingStartedAt !== null && run.thinkingMs === 0) {
    run.thinkingMs = Date.now() - run.thinkingStartedAt;
  }

  run.stopped = stopped;
  run.incomplete = isRunIncomplete(run);
  run.finished = true;

  touch(sessionId);
  return run;
}

/**
 * 标记该会话的内存态没能落库。
 *
 * 返回是否是首次标记——调用方据此只 console.error 一次，避免每次 flush 都刷屏。
 * 刻意不 touch()：调用方紧接着会 finishRun，由它统一触发渲染。
 */
export function markRunPersistFailed(sessionId: string): boolean {
  const run = runs.get(sessionId);
  if (!run || run.persistFailed) return false;

  run.persistFailed = true;
  return true;
}

/** 该会话最近一轮的内存态是否落库失败（后续 wave 可据此提示用户重试）。 */
export function isRunPersistFailed(sessionId: string): boolean {
  return Boolean(runs.get(sessionId)?.persistFailed);
}

/** 显式移除某个会话的 run（会话删除 / 清空）。 */
export function removeRun(sessionId: string): void {
  if (runs.delete(sessionId)) {
    // 版本记录可以删：版本号来自全局递增的 versionSeed，重建后不会退回旧值
    runVersions.delete(sessionId);
    touch(sessionId);
  }
}

/**
 * UI 已经从数据库读到最终内容，可以安全丢弃内存态。
 *
 * 仍有未处理的提问时保留内存态：那一轮的内容还没落库，清掉会让浮层失去依托。
 */
export function clearFinishedRun(sessionId: string): void {
  const run = runs.get(sessionId);
  if (!run?.finished) return;
  if (run.clarification) return;
  // 落库失败时 DB 里存的是旧内容（或什么都没有）：丢掉内存态等于用旧内容替换
  // 刚流出的回复，用户会看到回复消失。必须留着，等 UI 决定怎么处理。
  if (run.persistFailed) return;

  runs.delete(sessionId);
  touch(sessionId);
}

export function clearClarification(sessionId: string): void {
  const run = runs.get(sessionId);
  if (!run?.clarification) return;
  run.clarification = null;
  touch(sessionId);
}

/**
 * 追加一段给用户看的提示。
 *
 * 不属于协议事件的东西（网络失败、流被中断）都走这里，保证提示格式只有一处。
 */
export function appendNotice(sessionId: string, label: string, detail?: string): void {
  const run = runs.get(sessionId);
  if (!run) return;

  run.content += detail ? `\n\n> ⚠️ **${label}**: ${detail}` : `\n\n> ⚠️ **${label}**`;
  touch(sessionId);
}

/**
 * 记录一次「这一轮没跑完」的结构化失败。
 *
 * 与 `appendNotice` 的区别：除了写正文提示，还把 error 挂到 run 上——消息区据此
 * 显示「重试」按钮，落库后刷新也还能恢复。
 *
 * 没收到任何输出就把 sawError 置位：这不是「回复不完整」，而是本轮直接失败，不该
 * 再叠一条「回复不完整（已中断）」。中途断流、已经有半截内容时保持 incomplete，
 * 让用户知道看到的回答是不全的。
 */
export function setRunError(sessionId: string, error: RunError): void {
  const run = runs.get(sessionId);
  if (!run) return;

  const hadOutput =
    run.content.trim().length > 0 || run.thinking.trim().length > 0 || run.toolCalls.length > 0;
  run.error = error;
  if (!hadOutput) run.sawError = true;
  run.content += `\n\n> ⚠️ **请求失败**: ${error.message}`;
  touch(sessionId);
}

// ---------------------------------------------------------------------
// 事件折叠
// ---------------------------------------------------------------------
function upsertToolCall(
  run: Run,
  event: {
    tool_call_id: string;
    tool_name: string;
    args?: string;
    result?: string;
    outcome?: ToolOutcome;
  },
  status: ToolCallRecord['status'],
): void {
  const existing = run.toolCalls.find((call) => call.toolCallId === event.tool_call_id);

  if (existing) {
    existing.status = status;
    if (event.args !== undefined) existing.argsText = event.args;
    if (status === 'complete') {
      existing.result = event.result;
      // success 不落字段：老记录/旧服务端没有 outcome，默认就是成功
      existing.outcome =
        event.outcome && event.outcome !== 'success' ? event.outcome : undefined;
    }
    return;
  }

  run.toolCalls.push({
    toolCallId: event.tool_call_id,
    toolName: event.tool_name,
    argsText: event.args || '',
    result: status === 'complete' ? event.result : undefined,
    status,
    outcome:
      status === 'complete' && event.outcome && event.outcome !== 'success'
        ? event.outcome
        : undefined,
  });
}

/**
 * 把一个服务端事件折叠进 run 状态。
 *
 * 返回是否有**可见**变化：`done` 只更新内部标记，不值得触发一次渲染。
 */
export function applyEvent(sessionId: string, event: ChatEvent): boolean {
  const run = runs.get(sessionId);
  if (!run) return false;

  // 顺序表按需补建：老内存态或测试里手搓的 run 可能没有这个字段
  const order: PartKind[] = (run.partOrder ??= []);

  switch (event.type) {
    case 'thinking':
      if (run.thinkingStartedAt === null) run.thinkingStartedAt = Date.now();
      // 同一类块只会渲染一个：重复占位会让后面的工具调用错位；空增量不产生块，也不占位
      if (event.delta && !order.includes('reasoning')) order.push('reasoning');
      run.thinking += event.delta;
      break;

    case 'content':
      if (run.thinkingStartedAt !== null && run.thinkingMs === 0) {
        run.thinkingMs = Date.now() - run.thinkingStartedAt;
      }
      if (event.delta && !order.includes('text')) order.push('text');
      run.content += event.delta;
      break;

    case 'tool_call':
      // 每个新调用占一个位置（同名不同 id 是两个块）；同一 id 重发不重复占位
      if (!run.toolCalls.some((call) => call.toolCallId === event.tool_call_id)) {
        order.push('tool-call');
      }
      upsertToolCall(run, event, 'running');
      break;

    case 'tool_result':
      upsertToolCall(run, event, 'complete');
      break;

    case 'clarification':
      run.clarification = {
        toolCallId: event.tool_call_id,
        question: event.question,
        options: event.options ?? [],
        allowCustomInput: event.allow_custom_input !== false,
      };
      break;

    case 'fallback':
      // 独立字段而不是拼进正文：正文会作为助手历史再次发给模型，也会被复制/导出
      run.fallback = { fromModel: event.from_model, toModel: event.to_model };
      break;

    case 'usage':
      // 真实用量（不是估算）：上下文用了多少、可用上限多大、本轮生成多少
      run.usage = {
        contextTokens: event.context_tokens,
        contextWindow: event.context_window ?? null,
        contextLimitTokens: event.context_limit_tokens ?? null,
        outputTokens: event.output_tokens,
        cacheReadTokens: event.cache_read_tokens,
      };
      break;

    case 'error': {
      run.sawError = true;
      // 结构化记录供「重试」按钮与分类提示使用；正文里的提示保留，历史/复制时仍能看出这轮失败
      const code = readErrorCode(event.code);
      run.error = { message: event.message, retryable: event.retryable === true, code };
      // 上下文超限不是故障，而是一个需要用户换个会话的状态：标签跟着换，正文仍用
      // 服务端给的文案（里面带用量、上限与动作）。
      run.content += `\n\n> ⚠️ **${code === 'context_overflow' ? '上下文已满' : '错误'}**: ${event.message}`;
      break;
    }

    case 'done':
      run.sawDone = true;
      return false;

    case TRANSPORT_INCOMPLETE:
      // damaged 一经置位不再复位：丢过帧就是丢过帧，后文补上 done 也改变不了内容缺口
      run.damaged = run.damaged || event.damaged;
      run.incomplete = true;
      run.content += event.damaged
        ? '\n\n> ⚠️ **连接已中断**: 收到无法解析的流数据，回复可能不完整'
        : '\n\n> ⚠️ **连接已中断**: 回复可能不完整';
      break;

    default:
      // 未知事件（服务端新增类型时）被忽略且不触发渲染
      return false;
  }

  touch(sessionId);
  return true;
}
