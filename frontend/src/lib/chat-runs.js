import { db } from '../db';

// 会话运行管理器
//
// 生成任务的生命周期挂在模块上而不是 React 组件上：
// 切换会话、组件卸载都不会中断请求，回到该会话时可以直接接着看流式输出。
// UI 通过订阅这里的版本号来获取最新状态。

const runs = new Map(); // sessionId -> run
const listeners = new Set();

let version = 0;
let sessionsChangedHandler = null;

function emit() {
  version += 1;
  for (const listener of listeners) listener();
}

export function subscribeRuns(listener) {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** 每次运行状态或增量内容变化都会自增，配合 useSyncExternalStore 触发重渲染 */
export function getRunsVersion() {
  return version;
}

/** App 注册：会话列表需要刷新时（标题/排序变化）回调 */
export function setSessionsChangedHandler(handler) {
  sessionsChangedHandler = handler;
}

export function notifySessionsChanged() {
  sessionsChangedHandler?.();
}

export function getRun(sessionId) {
  return runs.get(sessionId) ?? null;
}

export function isRunActive(sessionId) {
  const run = runs.get(sessionId);
  return Boolean(run && !run.finished);
}

/** 仍在生成中的会话 id（已结束但 UI 还没取走结果的不算） */
export function getRunningSessionIds() {
  const ids = [];
  for (const run of runs.values()) {
    if (!run.finished) ids.push(run.sessionId);
  }
  return ids;
}

/** UI 已经从数据库读到最终内容，可以安全丢弃内存态 */
export function clearFinishedRun(sessionId) {
  const run = runs.get(sessionId);
  if (!run?.finished) return;
  // 还有没被用户回答的意图提问：保留内存态，等用户选择/输入或忽略后再清理
  if (run.clarification && !run.clarification.handled) return;
  runs.delete(sessionId);
  emit();
}

/**
 * 标记澄清提问已处理：用户点选选项、手动输入提交，或者直接忽略关闭时调用。
 * - 内存态立即生效（同步），保证调用方紧接着就能发起下一轮对话；
 * - 同时异步落库，避免刷新页面 / 切换会话后又一次弹出同一个提问。
 */
export function dismissClarification(sessionId) {
  const run = runs.get(sessionId);
  if (run?.clarification) {
    run.clarification.handled = true;
  }
  // 提问已经收尾，此时才真正释放已结束的内存态
  if (run?.finished) {
    runs.delete(sessionId);
  }
  emit();

  void (async () => {
    try {
      const records = await db.messages
        .where('sessionId')
        .equals(sessionId)
        .sortBy('createdAt');
      for (let i = records.length - 1; i >= 0; i -= 1) {
        const record = records[i];
        if (record.role !== 'assistant') continue;
        if (record.clarification && !record.clarification.handled) {
          await db.messages.update(record.id, {
            clarification: { ...record.clarification, handled: true },
          });
        }
        break;
      }
    } catch (err) {
      console.warn('标记意图提问已处理失败:', err);
    }
  })();
}

export function abortChatRun(sessionId) {
  const run = runs.get(sessionId);
  if (!run || run.finished) return;
  run.controller.abort();
}

/**
 * 启动一次生成。同一会话同时只允许一个运行中的任务。
 * 返回 run 对象（含 assistantMsgId 等），已在运行时返回 null。
 */
export function startChatRun({ sessionId, assistantMsgId, model, reasoningEffort }) {
  if (isRunActive(sessionId)) return null;

  const run = {
    sessionId,
    assistantMsgId,
    model,
    reasoningEffort,
    controller: new AbortController(),
    startedAt: Date.now(),
    thinking: '',
    content: '',
    thinkingMs: 0,
    toolCalls: [],
    stopped: false,
    finished: false,
  };

  runs.set(sessionId, run);
  emit();

  // 故意不 await：调用方（onNew / onReload）不需要等整段流结束
  void executeRun(run);

  return run;
}

async function executeRun(run) {
  const { sessionId, assistantMsgId, model, reasoningEffort, controller } = run;

  let thinkingStartedAt = null;
  let lastFlushAt = 0;
  let aborted = false;

  // 流式过程中节流落库：刷新/关闭页面时不至于丢掉整段回复
  const flushPartial = () => {
    const now = Date.now();
    if (now - lastFlushAt < 800) return;
    lastFlushAt = now;
    db.messages
      .update(assistantMsgId, {
        content: run.content,
        thinking: run.thinking,
        thinkingMs: run.thinkingMs,
        toolCalls: run.toolCalls,
      })
      .catch(() => {});
  };

  try {
    // 历史上下文：去掉本轮占位消息与空助手消息，避免把脏数据传给模型
    const allHistory = await db.messages
      .where('sessionId')
      .equals(sessionId)
      .sortBy('createdAt');

    const contextMessages = allHistory
      .filter((m) => m.id !== assistantMsgId)
      .filter((m) => m.role !== 'assistant' || (m.content && m.content.trim()))
      .map((m) => ({
        role: m.role,
        content: m.content,
        thinking: m.thinking || null,
      }));

    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model,
        reasoning_effort: reasoningEffort,
        messages: contextMessages,
        stream: true,
      }),
      signal: controller.signal,
    });

    if (!response.ok) throw new Error(`HTTP 错误: ${response.status}`);

    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();

      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed || !trimmed.startsWith('data:')) continue;
        const dataStr = trimmed.substring(5).trim();
        if (!dataStr) continue;

        let event;
        try {
          event = JSON.parse(dataStr);
        } catch (err) {
          console.error('SSE JSON 解析错误:', err);
          continue;
        }

        if (event.type === 'thinking') {
          if (thinkingStartedAt === null) thinkingStartedAt = Date.now();
          run.thinking += event.delta;
        } else if (event.type === 'content') {
          if (thinkingStartedAt !== null && run.thinkingMs === 0) {
            run.thinkingMs = Date.now() - thinkingStartedAt;
          }
          run.content += event.delta;
        } else if (event.type === 'tool_call') {
          // 工具开始调用
          const tcId = event.tool_call_id;
          const existing = run.toolCalls.find((t) => t.toolCallId === tcId);
          if (!existing) {
            run.toolCalls.push({
              toolCallId: tcId,
              toolName: event.tool_name,
              argsText: event.args || '',
              result: undefined,
              status: 'running',
            });
          }
          if (event.tool_name === 'ask_user_clarification') {
            run.clarification = {
              toolCallId: tcId,
              argsText: event.args || '',
              handled: false,
            };
          }
        } else if (event.type === 'tool_result') {
          // 工具调用完成
          const tcId = event.tool_call_id;
          const existing = run.toolCalls.find((t) => t.toolCallId === tcId);
          if (existing) {
            existing.result = event.result;
            existing.status = 'complete';
          } else {
            run.toolCalls.push({
              toolCallId: tcId,
              toolName: event.tool_name,
              argsText: '',
              result: event.result,
              status: 'complete',
            });
          }
        } else if (event.type === 'error') {
          run.content += `\n\n> ⚠️ **错误**: ${event.message}`;
        } else {
          continue;
        }

        emit();
        flushPartial();
      }
    }
  } catch (err) {
    if (err.name === 'AbortError') {
      // 用户停止 / 会话被删除：保留已收到的内容并标记为已停止
      aborted = true;
    } else {
      run.content += `\n\n> ⚠️ **请求失败**: ${err.message}`;
    }
  } finally {
    if (thinkingStartedAt !== null && run.thinkingMs === 0) {
      run.thinkingMs = Date.now() - thinkingStartedAt;
    }
    run.stopped = aborted;
    run.finished = true;

    // 如果 AI 发起了澄清提问，且没有输出正文内容或正文为空，将提问内容填入 content，确保气泡与历史上下文连贯
    if (run.clarification && (!run.content || !run.content.trim())) {
      try {
        const parsed = JSON.parse(run.clarification.argsText || '{}');
        if (parsed.question) {
          run.content = parsed.question;
        }
      } catch {}
    }

    try {
      await db.messages.update(assistantMsgId, {
        content: run.content,
        thinking: run.thinking,
        thinkingMs: run.thinkingMs,
        toolCalls: run.toolCalls,
        stopped: aborted,
        // 持久化意图提问，页面刷新或切换会话后仍可恢复待回答的选项浮层
        clarification: run.clarification
          ? {
              argsText: run.clarification.argsText,
              handled: Boolean(run.clarification.handled),
            }
          : null,
      });
      await db.sessions.update(sessionId, { updatedAt: Date.now() });
      notifySessionsChanged();
    } catch (dbErr) {
      console.warn('持久化消息记录失败:', dbErr);
    }

    emit();
  }
}
