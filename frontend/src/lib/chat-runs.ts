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
 *
 * 跨标签页：生成路径由 `multi-tab` 的 run 锁互斥——同一会话同时只有一页在生成，另一页
 * 立即以错误收尾；run 收尾后广播失效通知，别的页据此重读会话列表与消息。
 * **已知限制**：跨标签同时手动改动同一批旧消息仍是后写覆盖（IndexedDB 不提供跨页事务
 * 协调），本轮只把生成路径互斥掉。
 */

import { db } from '../db';
import { HISTORY_LIMIT, PARTIAL_FLUSH_INTERVAL_MS } from './constants';
import {
  clearMessageApproval,
  clearMessageClarification,
  loadContextMessages,
  loadSessionMessages,
  saveFinal,
  saveProgress,
  touchSession,
} from './chat-persistence';
import {
  activeSessionIds,
  abortRun,
  applyEvent,
  appendNotice,
  clearClarification,
  createRun,
  finishRun,
  getRun,
  markRunPersistFailed,
  setRunError,
} from './chat-store';
import { ChatRequestError } from './chat-request-error';
import { streamChat } from './chat-transport';
import { describeConnectionFailure } from './connection-failure';
import { acquireRunLock, notifySessionsChanged as broadcastToOtherTabs } from './multi-tab';
import type { RunState } from './chat-store';
import type { ContextToolCall } from './chat-persistence';
import type { MessageRecord } from '../types';

// 对外保持单一入口：消费方只 import 本模块
export {
  getRun,
  isRunActive,
  isRunPersistFailed,
  getRunVersion,
  subscribeRun,
  getRunningSessionsKey,
  subscribeRunningSessions,
  removeRun as removeSessionRun,
  clearFinishedRun,
  abortRun as abortChatRun,
} from './chat-store';
export {
  buildLiveMessage,
  toRuntimeMessage,
  pendingApproval,
  pendingClarification,
  contextExhausted,
  type PendingApproval,
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
/**
 * 立即把所有进行中 run 的当前内容落库，绕过 `PARTIAL_FLUSH_INTERVAL_MS` 节流。
 *
 * 页面隐藏/卸载时调用：那时定时器不一定还能跑，等下一次节流窗口等于丢掉最多
 * 800ms 的增量（用户看到的是「最后一句没写完」）。已完成（或不存在）的 run
 * 跳过——它们的终态由 saveFinal 负责。
 */
export function flushAllRunsNow(): void {
  for (const sessionId of activeSessionIds()) {
    const run = getRun(sessionId);
    if (!run || run.finished) continue;
    saveProgress(run.assistantMsgId, run).catch((err: unknown) =>
      console.warn('流式进度落库失败:', err),
    );
  }
}

/**
 * 中止所有在途生成（登出时用：旧账号的流不该在新账号登录前继续往库里写）。
 *
 * 不 await 各 run 的收尾，也不删 run 对象：
 *   - 收尾由 `executeRun` 的 finally 自己完成（落库 → finishRun），删掉 run 反而会
 *     让 UI 丢掉内存态；
 *   - 中止后那次终态落库是 `db.messages.update`，**行已被清空时是空操作**，
 *     所以只要调用方在其后清库（`clearAllLocalData`），就不会把旧账号的内容写回来。
 */
export function abortAllRuns(): void {
  for (const sessionId of activeSessionIds()) {
    abortRun(sessionId);
  }
}

/**
 * 注册「离开页面 / 切到后台」时的补写钩子。返回解绑函数（测试与显式卸载用）。
 *
 * 必须在这里（模块级）注册而不是 React 组件里：run 的生命周期挂在模块上，切换
 * 会话、组件卸载都不会中断请求，钩子也不能跟着某个组件一起消失。
 * node 环境没有 window/document，直接短路——vitest 的 node environment 走这条路。
 */
export function registerLifecycleFlush(): () => void {
  if (typeof window === 'undefined' || typeof document === 'undefined') return () => {};

  const onPageHide = (): void => flushAllRunsNow();
  const onVisibilityChange = (): void => {
    if (document.visibilityState === 'hidden') flushAllRunsNow();
  };

  window.addEventListener('pagehide', onPageHide);
  document.addEventListener('visibilitychange', onVisibilityChange);

  return () => {
    window.removeEventListener('pagehide', onPageHide);
    document.removeEventListener('visibilitychange', onVisibilityChange);
  };
}

export interface StartChatRunParams {
  sessionId: string;
  assistantMsgId: string;
  model: string | null;
  reasoningEffort: string | null;
  /**
   * 审批续跑：`tool_call_id` → 是否批准。
   *
   * 带上它就改变了这次请求的形状：历史必须以一条带工具调用的助手消息结尾，
   * 并且把决定发回服务端。其余情况为 undefined。
   */
  approvals?: Record<string, boolean> | null;
  /**
   * 续跑时要回放的待审批调用（**从内存/库里取好的快照**）。
   *
   * 刻意由调用方显式传入，而不是让 `loadContextMessages` 从库里读 `approval`：
   * 那个字段必须在本轮起跑前清掉（否则下一轮会重复回放它），于是「清掉」与
   * 「读到」之间就有一场 IndexedDB 读写竞态——快一步读不到，慢一步又读到已经
   * 不该出现的数据。传快照让这件事变成确定性的。
   */
  resumeToolCalls?: ContextToolCall[] | null;
}

// ---------------------------------------------------------------------
// 跨标签失效后的消息重读
// ---------------------------------------------------------------------
/**
 * 请求把某个会话的消息列表从库里重读一次（跨标签失效通知调用）。
 *
 * 为什么绕成「造一个终态 run」：ChatProvider 里唯一一条「库里变了 → 重读整表」的通道是
 * 「本会话存在一个**已结束**的 run」（生成结束后的回读 + 丢弃内存态）。跨标签通知没有
 * 别的入口能触达它内部的 dbMessages，而本轮不动 ChatProvider，于是借这条既有通道发信号
 * ——这个 run 只活在内存里，绝不会走 saveFinal，库不会被它写坏。
 *
 * 覆盖层按库里那条记录逐字段摆好：回读要等一次 IndexedDB 往返，覆盖层若是空的，用户会
 * 看到已完成的回复先变空再变回来。
 *
 * 本页已有 run 内存态时直接跳过：本页自己的收尾会走同一条回读；而落库失败（persistFailed）
 * 的内存态是那份内容的唯一副本，更不能被顶掉。
 */
export async function requestSessionMessagesReload(sessionId: string): Promise<void> {
  if (getRun(sessionId)) return;

  let records: MessageRecord[];
  try {
    records = await loadSessionMessages(sessionId);
  } catch (err) {
    console.warn('跨标签重读会话消息失败:', err);
    return;
  }

  // 只覆盖助手消息：覆盖层按助手消息渲染，借用户消息的 id 会把提问画成回答
  const target = findLastAssistantRecord(records);
  if (!target) return;
  // 读库期间本页可能已经起了自己的 run（用户点了发送/重新生成），再确认一次
  if (getRun(sessionId)) return;

  const run = createRun({
    sessionId,
    assistantMsgId: target.id,
    model: null,
    reasoningEffort: null,
  });
  if (!run) return;

  run.content = target.content ?? '';
  run.thinking = target.thinking ?? '';
  run.thinkingMs = target.thinkingMs ?? 0;
  run.toolCalls = target.toolCalls ?? [];
  run.partOrder = target.partOrder ?? [];
  run.clarification = target.clarification ?? null;
  // 记录已经落库：它是否不完整按库里的取值，不能按「没收全」判成半截
  run.sawDone = true;

  const finished = finishRun(sessionId, { stopped: Boolean(target.stopped) });
  if (finished) finished.incomplete = Boolean(target.incomplete);
}

function findLastAssistantRecord(records: readonly MessageRecord[]): MessageRecord | null {
  for (let i = records.length - 1; i >= 0; i -= 1) {
    const record = records[i];
    if (record?.role === 'assistant') return record;
  }
  return null;
}

/**
 * 启动一次生成。同一会话同时只允许一个运行中的任务。
 *
 * 返回 run 对象（含 assistantMsgId 等），已在运行时返回 null。
 */
export function startChatRun(params: StartChatRunParams): RunState | null {
  const run = createRun(params);
  if (!run) return null;

  // 故意不 await：调用方（onNew / onReload）不需要等整段流结束
  void executeRun(run, params.approvals ?? null, params.resumeToolCalls ?? null);
  return run;
}

async function executeRun(
  run: RunState,
  approvals: Record<string, boolean> | null,
  resumeToolCalls: ContextToolCall[] | null,
): Promise<void> {
  const { sessionId, assistantMsgId, model, reasoningEffort, controller } = run;

  // 跨标签互斥先于一切副作用：拿不到锁说明另一个标签页正在同一会话里生成，那条助手消息
  // 归它——本次 run 直接以终端错误收尾，既不发请求也不落库（写进去只会互相整段覆写）。
  const lock = await acquireRunLock(sessionId);
  if (lock.kind === 'busy') {
    // 提示必须落库：只写内存的话，随后的 finished-run 回读会把它丢掉，用户只看到
    // 「消息发出去了、永远没有回复」。共享同一条助手记录（重生成竞态）时，持锁方
    // 会在自己的 saveFinal 里覆盖这里，最终内容仍以持锁方为准。
    appendNotice(sessionId, '另一个标签页正在生成回复，本次未发送', '请等待它完成后再重试。');
    run.stopped = true;
    try {
      await saveFinal(assistantMsgId, run);
    } catch (err) {
      console.warn('跨标签取消提示落库失败:', err);
    }
    finishRun(sessionId, { stopped: true });
    return;
  }

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
    const resuming = approvals !== null && Object.keys(approvals).length > 0;
    const { messages: loaded, anchor: contextAnchor } = await loadContextMessages(sessionId, {
      excludeId: assistantMsgId,
      limit: HISTORY_LIMIT,
    });
    // 续跑：历史必须以「带工具调用的助手消息」结尾（服务端据此重建要续跑什么）。
    // 这条消息由调用方给的快照**构造**出来，不从库里读——理由见 `resumeToolCalls`。
    const contextMessages =
      resuming && resumeToolCalls && resumeToolCalls.length > 0
        ? [...loaded, { role: 'assistant', content: '', tool_calls: resumeToolCalls }]
        : loaded;

    for await (const event of streamChat({
      model,
      reasoningEffort,
      messages: contextMessages,
      // 上一轮上游回报的真实用量：有了它，服务端只估算它之后的那几条消息
      contextAnchor,
      approvals: resuming ? approvals : null,
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
      // 原始错误只进日志：Chrome 的「Failed to fetch」、Safari 的「Load failed」不是
      // 给用户看的文案。翻译后挂到 run.error 上，消息区才会出现「重试」按钮。
      const failure = describeConnectionFailure(err);
      // 429 带 `Retry-After` 时把「等到什么时候」一起带上：否则用户点「重试」只会
      // 立刻再吃一次 429，而按钮本身没有任何解释。
      const retryAfterAt =
        err instanceof ChatRequestError && err.retryAfterSeconds !== null
          ? Date.now() + err.retryAfterSeconds * 1000
          : null;
      setRunError(sessionId, {
        message: failure.message,
        retryable: failure.retryable,
        code: null,
        retryAfterAt,
      });
    }
  } finally {
    // saveFinal 会把 stopped 落库，先同步到 run 上，避免「内存已停止、库里仍是 false」
    run.stopped = stopped;

    try {
      await saveFinal(assistantMsgId, run);
      await touchSession(sessionId);
    } catch (err) {
      // 落库失败时 DB 里的内容已不可信（可能是旧一轮的内容，甚至什么都没有）。
      // 标记后 finishRun 不会让 clearFinishedRun 丢掉内存态，UI 才能继续显示刚流出的回复。
      if (markRunPersistFailed(sessionId)) {
        console.error('消息落库失败，已保留内存态:', err);
      }
    }
    // 终态已落库（或已确认落不进去）：通知其他标签页重读。只广播 id，内容由它们自己去库
    broadcastToOtherTabs(sessionId);
    notifySessionsChanged();

    // 无论成功、失败还是中止，都必须在落库完成后再 finishRun，
    // 避免 ChatProvider loadMessages 读到未落盘状态并删掉 overlay 导致界面闪烁空白
    finishRun(sessionId, { stopped });

    // 持锁到 run 真正结束：放这里，中途 return、抛错都不会把锁漏在自己手里
    if (lock.kind === 'held') lock.release();
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

// 模块加载即挂上「关页/切后台补写」：发生在任何组件挂载之前（也没有组件能保证
// 在页面隐藏时还活着）。
registerLifecycleFlush();

// ---------------------------------------------------------------------
// 审批续跑
// ---------------------------------------------------------------------
/**
 * 用户对一批待审批调用做出决定后，续跑那一轮。
 *
 * ## 为什么是「重跑同一条助手消息」而不是发一条新消息

    被拦下的那一轮**没有产生新的用户提问**（用户只是点了「批准」）。如果像普通消息那样
    先追加一条 user 消息，历史里就会多出一句用户根本没说过的话，模型会看到「他批准了
    什么？」这种不存在的追问。

    所以复用那条助手消息：清掉它的内容与待审批标记，把决定带进请求，让它重新生成为
    完整的回答。`includeExcluded` 确保那条带 `tool_calls` 的消息仍在回放历史里——
    服务端要靠它重建「要续跑什么」。

    ## 为什么先清 `approval` 再起跑

    `loadContextMessages` 会把带 `approval.toolCalls` 的消息回放出去。清得太晚，
    下一次请求会重复带上已经决定过的调用（服务端会把它当成新的待审批，于是一次又一次
    弹同一个审批框）。清理与起跑的顺序因此不能反。

    ## 失败时

    起跑失败（槽位被占）不动任何数据：用户还能再点一次。落库失败只记日志——那一轮的
    内存态仍是完整内容，终态落库会盖过去。
 */
export async function resumeApprovalRun({
  sessionId,
  assistantMsgId,
  decisions,
  toolCalls,
  model,
  reasoningEffort,
}: {
  sessionId: string;
  assistantMsgId: string;
  /** `tool_call_id` → 是否批准。必须覆盖该条消息上的**全部**待审批调用。 */
  decisions: Record<string, boolean>;
  /** 本次续跑要回放的待审批调用（调用方从消息 metadata 里取好的快照）。 */
  toolCalls: ContextToolCall[];
  model: string | null;
  reasoningEffort: string | null;
}): Promise<boolean> {
  if (Object.keys(decisions).length === 0) return false;

  const started = startChatRun({
    sessionId,
    assistantMsgId,
    model,
    reasoningEffort,
    approvals: decisions,
    resumeToolCalls: toolCalls,
  });
  if (!started) return false;

  try {
    await db.messages.update(assistantMsgId, {
      content: '',
      thinking: '',
      thinkingMs: 0,
      toolCalls: [],
      stopped: false,
      clarification: null,
      // 决定已经带进这次请求：待审批标记必须清掉，否则下一轮还会回放它
      approval: null,
      fallback: null,
      error: null,
    });
  } catch (err) {
    // 清不掉不影响本轮生成：内存覆盖层与终态落库都会盖过去
    console.warn('审批续跑：清空旧内容失败:', err);
  }

  return true;
}

/**
 * 用户不处理这批审批调用：清掉标记，本轮作废。
 *
 * 与「拒绝」的区别必须清楚：拒绝是一次**明确的决定**，会带着 `false` 续跑，模型因此
 * 知道该操作没被允许并能改走别的路；而 dismiss 是「用户不想现在决定」，续跑不会发生，
 * 那一轮就停在那里。把 dismiss 也当成拒绝去续跑，用户会看到一条自己没要求的新回答。
 */
export function dismissApproval(messageId: string): void {
  if (!messageId) return;
  clearMessageApproval(messageId).catch((err: unknown) =>
    console.warn('清除待审批调用失败:', err),
  );
}
