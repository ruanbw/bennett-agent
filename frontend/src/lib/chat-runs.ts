/**
 * 生成任务的编排门面（R-4 拆分之四）。
 *
 * 变化理由：编排策略——什么顺序、什么条件下做什么。此前这个文件同时承担传输、
 * 内存状态、持久化与会话列表通知，改任何一层都要动它。
 *
 * 现在职责分层：
 *   - 传输：`chat-transport`（fetch + SSE 分帧）
 *   - 状态：`chat-store`（run 状态机 + 订阅）
 *   - 存储：`chat-persistence`（Dexie 读写形状）
 *   - 编排：本文件
 *
 * 生成任务的生命周期因此仍然挂在模块上而不是 React 组件上：切换会话、组件卸载
 * 都不会中断请求，回到该会话时可以直接接着看流式输出。
 */

import { HISTORY_LIMIT, PARTIAL_FLUSH_INTERVAL_MS } from './constants';
import {
  clearMessageClarification,
  loadContextMessages,
  saveFinal,
  saveProgress,
  touchSession,
} from './chat-persistence';
import { applyEvent, appendNotice, clearClarification, createRun, finishRun } from './chat-store';
import { streamChat } from './chat-transport';
import type { Run } from '../types';

// 对外保持单一入口：消费方只 import 本模块
export {
  getRun,
  isRunActive,
  getRunVersion,
  subscribeRun,
  getRunningSessionsKey,
  getRunningSessionIds,
  subscribeRunningSessions,
  removeRun as removeSessionRun,
  clearFinishedRun,
  abortRun as abortChatRun,
} from './chat-store';
export {
  buildLiveMessage,
  toRuntimeMessage,
  pendingClarification,
  type PendingClarification,
} from './chat-message';

// ---------------------------------------------------------------------
// 会话列表刷新通知
//
// 与 run 状态无关：标题、updatedAt 变化时 App 需要重载列表。这里只是一个
// 单槽回调，不引入订阅者集合。
// ---------------------------------------------------------------------
let sessionsChangedHandler: (() => void) | null = null;

/** App 注册：会话列表需要刷新时（标题/排序变化）回调。 */
export function setSessionsChangedHandler(handler: (() => void) | null): void {
  sessionsChangedHandler = handler;
}

export function notifySessionsChanged(): void {
  sessionsChangedHandler?.();
}

// ---------------------------------------------------------------------
// 编排
// ---------------------------------------------------------------------
export interface StartChatRunParams {
  sessionId: string;
  assistantMsgId: string;
  model: string | null;
  reasoningEffort: string | null;
}

/**
 * 启动一次生成。同一会话同时只允许一个运行中的任务。
 *
 * 返回 run 对象（含 assistantMsgId 等），已在运行时返回 null。
 */
export function startChatRun(params: StartChatRunParams): Run | null {
  const run = createRun(params);
  if (!run) return null;

  // 故意不 await：调用方（onNew / onReload）不需要等整段流结束
  void executeRun(run);
  return run;
}

async function executeRun(run: Run): Promise<void> {
  const { sessionId, assistantMsgId, model, reasoningEffort, controller } = run;

  let lastFlushAt = 0;
  const flushPartial = (): void => {
    const now = Date.now();
    if (now - lastFlushAt < PARTIAL_FLUSH_INTERVAL_MS) return;
    lastFlushAt = now;
    // 落库失败不该中断生成
    saveProgress(assistantMsgId, run).catch((err: unknown) => console.warn('流式进度落库失败:', err));
  };

  let stopped = false;

  try {
    const contextMessages = await loadContextMessages(sessionId, {
      excludeId: assistantMsgId,
      limit: HISTORY_LIMIT,
    });

    for await (const event of streamChat({
      model,
      reasoningEffort,
      messages: contextMessages,
      signal: controller.signal,
    })) {
      if (applyEvent(sessionId, event)) flushPartial();
    }
  } catch (err) {
    if (err instanceof Error && err.name === 'AbortError') {
      // 用户停止 / 会话被删除：保留已收到的内容并标记为已停止
      stopped = true;
    } else {
      console.error('生成请求失败:', err);
      appendNotice(sessionId, '请求失败', err instanceof Error ? err.message : String(err));
    }
  } finally {
    // 无论成功、失败还是中止，都必须收尾，否则 UI 会永远停在「生成中」
    finishRun(sessionId, { stopped });

    try {
      await saveFinal(assistantMsgId, run);
      await touchSession(sessionId);
      notifySessionsChanged();
    } catch (err) {
      console.warn('持久化消息记录失败:', err);
    }
  }
}

/**
 * 用户忽略提问。
 *
 * 语义就是「记录上不再带 clarification」——`pendingClarification` 的推导条件因此
 * 不成立。改动前需要同时维护内存里的 `handled` 标记、一个 `resolvedClarificationIds`
 * 集合，以及刷新后的恢复循环，三者都在补偿「状态没有推导来源」。
 */
export function dismissClarification(sessionId: string, messageId: string): void {
  clearClarification(sessionId);
  if (!messageId) return;

  clearMessageClarification(messageId).catch((err: unknown) =>
    console.warn('清除待答提问失败:', err),
  );
}
