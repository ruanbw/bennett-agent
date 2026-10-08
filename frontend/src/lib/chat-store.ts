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
 */

import { NOTIFY_COALESCE_MS } from './constants';
import { TRANSPORT_INCOMPLETE, type ChatEvent, type Run, type ToolCallRecord } from '../types';

type Listener = () => void;

const runs = new Map<string, Run>();
const runListeners = new Map<string, Set<Listener>>();
const runningListeners = new Set<Listener>();
const runVersions = new Map<string, number>();

let runningKey = '';
let flushTimer: ReturnType<typeof setTimeout> | null = null;
const dirtySessions = new Set<string>();

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

export function getRunningSessionIds(): string[] {
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
    runVersions.set(sessionId, (runVersions.get(sessionId) ?? 0) + 1);
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
export function getRun(sessionId: string): Run | null {
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
 */
export function createRun({
  sessionId,
  assistantMsgId,
  model,
  reasoningEffort = null,
}: CreateRunParams): Run | null {
  if (isRunActive(sessionId)) return null;

  const previous = runs.get(sessionId);
  if (previous?.finished) runs.delete(sessionId);

  const run: Run = {
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
    clarification: null,
    sawDone: false,
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

export function finishRun(sessionId: string, { stopped = false } = {}): Run | null {
  const run = runs.get(sessionId);
  if (!run) return null;

  // 思考计时的收尾：正文出现过就已在 applyEvent 里结算，这里兜住「只有思考没有正文」
  if (run.thinkingStartedAt !== null && run.thinkingMs === 0) {
    run.thinkingMs = Date.now() - run.thinkingStartedAt;
  }

  run.stopped = stopped;
  run.incomplete = !run.sawDone && !stopped;
  run.finished = true;

  touch(sessionId);
  return run;
}

/** 显式移除某个会话的 run（会话删除 / 清空）。 */
export function removeRun(sessionId: string): void {
  if (runs.delete(sessionId)) {
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

// ---------------------------------------------------------------------
// 事件折叠
// ---------------------------------------------------------------------
function upsertToolCall(
  run: Run,
  event: { tool_call_id: string; tool_name: string; args?: string; result?: string },
  status: ToolCallRecord['status'],
): void {
  const existing = run.toolCalls.find((call) => call.toolCallId === event.tool_call_id);

  if (existing) {
    existing.status = status;
    if (event.args !== undefined) existing.argsText = event.args;
    if (status === 'complete') existing.result = event.result;
    return;
  }

  run.toolCalls.push({
    toolCallId: event.tool_call_id,
    toolName: event.tool_name,
    argsText: event.args || '',
    result: status === 'complete' ? event.result : undefined,
    status,
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

  switch (event.type) {
    case 'thinking':
      if (run.thinkingStartedAt === null) run.thinkingStartedAt = Date.now();
      run.thinking += event.delta;
      break;

    case 'content':
      if (run.thinkingStartedAt !== null && run.thinkingMs === 0) {
        run.thinkingMs = Date.now() - run.thinkingStartedAt;
      }
      run.content += event.delta;
      break;

    case 'tool_call':
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

    case 'error':
      run.content += `\n\n> ⚠️ **错误**: ${event.message}`;
      break;

    case 'done':
      run.sawDone = true;
      return false;

    case TRANSPORT_INCOMPLETE:
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
