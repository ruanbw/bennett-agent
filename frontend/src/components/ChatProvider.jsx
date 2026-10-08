import React, {
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react';
import {
  AssistantRuntimeProvider,
  useExternalStoreRuntime,
} from '@assistant-ui/react';
import { db } from '../db';
import {
  abortChatRun,
  clearFinishedRun,
  dismissClarification,
  getRun,
  getRunsVersion,
  isRunActive,
  notifySessionsChanged,
  startChatRun,
  subscribeRuns,
} from '../lib/chat-runs';

// 把 IndexedDB 记录转换成 runtime 消息（过滤掉内部意图确认工具，不污染历史聊天列表）
function toRuntimeMessage(record) {
  const content = [];

  // 工具调用部件
  if (Array.isArray(record.toolCalls)) {
    for (const tc of record.toolCalls) {
      if (tc.toolName === 'ask_user_clarification') continue;
      content.push({
        type: 'tool-call',
        toolName: tc.toolName || 'tool',
        toolCallId: tc.toolCallId || String(Math.random()),
        argsText: tc.argsText || '',
        result: tc.result,
        status: { type: tc.status || 'complete' },
      });
    }
  }

  // 思考过程部件
  if (record.thinking) {
    content.push({
      type: 'reasoning',
      text: record.thinking,
      status: { type: 'complete' },
    });
  }

  // 最终文本内容
  if (record.content) {
    content.push({
      type: 'text',
      text: record.content,
      status: { type: 'complete' },
    });
  }

  const message = {
    id: record.id,
    role: record.role,
    content,
    createdAt: new Date(record.createdAt),
    metadata: {
      custom: {
        stopped: Boolean(record.stopped),
        thinkingMs: record.thinkingMs ?? 0,
        clarification: record.clarification ?? null,
      },
    },
  };
  if (record.role === 'assistant') {
    message.status = { type: 'complete' };
  }
  return message;
}

// 生成中的消息：内容直接来自内存里的 run，随增量变化（过滤掉内部意图确认工具）
function buildLiveMessage(run) {
  const reasoningDone = Boolean(run.content) || run.finished;
  const content = [];

  // 正在或已执行的工具调用部件
  if (Array.isArray(run.toolCalls)) {
    for (const tc of run.toolCalls) {
      if (tc.toolName === 'ask_user_clarification') continue;
      content.push({
        type: 'tool-call',
        toolName: tc.toolName || 'tool',
        toolCallId: tc.toolCallId || String(Math.random()),
        argsText: tc.argsText || '',
        result: tc.result,
        status: { type: tc.status || 'complete' },
      });
    }
  }

  // 思考部件
  if (run.thinking) {
    content.push({
      type: 'reasoning',
      text: run.thinking,
      status: { type: reasoningDone ? 'complete' : 'running' },
    });
  }

  // 文本内容部件
  if (run.content) {
    content.push({
      type: 'text',
      text: run.content,
      status: { type: run.finished ? 'complete' : 'running' },
    });
  }

  return {
    id: run.assistantMsgId,
    role: 'assistant',
    content,
    createdAt: new Date(run.startedAt),
    status: run.finished ? { type: 'complete' } : undefined,
    metadata: {
      custom: { stopped: run.stopped, thinkingMs: run.thinkingMs },
    },
  };
}

// 解析模型通过 ask_user_clarification 工具传入的提问参数
function parseClarificationArgs(argsText) {
  try {
    const parsed = JSON.parse(argsText || '{}');
    if (!parsed?.question) return null;
    return {
      question: parsed.question,
      options: Array.isArray(parsed.options) ? parsed.options : [],
      allow_custom_input: parsed.allow_custom_input !== false,
    };
  } catch {
    return null;
  }
}

export function ChatProvider({ children, currentSessionId, currentModel, currentReasoningEffort }) {
  const [dbMessages, setDbMessages] = useState([]);
  const messageSeqRef = useRef(0);
  // 当前待回答的意图提问所属的助手消息 id
  const activeClarificationMsgIdRef = useRef(null);
  // 本轮会话中已处理过的提问 id（避免内存态未及时刷新时重复弹出）
  const [resolvedClarificationIds, setResolvedClarificationIds] = useState(() => new Set());

  // 订阅运行管理器：任何增量/状态变化都会让这里重渲染
  useSyncExternalStore(subscribeRuns, getRunsVersion);
  const run = getRun(currentSessionId);
  const isRunning = Boolean(run && !run.finished);

  // 单调递增的时间戳：同一毫秒内连续发送也不会导致消息错序
  const nextTimestamp = useCallback(() => {
    const now = Date.now();
    messageSeqRef.current = now <= messageSeqRef.current ? messageSeqRef.current + 1 : now;
    return messageSeqRef.current;
  }, []);

  // 从 IndexedDB 读取当前会话消息
  const loadMessages = useCallback(async () => {
    const records = await db.messages
      .where('sessionId')
      .equals(currentSessionId)
      .sortBy('createdAt');
    const last = records[records.length - 1];
    if (last) {
      messageSeqRef.current = Math.max(messageSeqRef.current, last.createdAt);
    }
    return records.map(toRuntimeMessage);
  }, [currentSessionId]);

  useEffect(() => {
    let active = true;
    loadMessages().then((list) => {
      if (active) setDbMessages(list);
    });
    return () => {
      active = false;
    };
  }, [loadMessages]);

  // 生成结束后：把最终内容从数据库读回来，再丢弃内存态
  // （若仍有未回答的意图提问，clearFinishedRun 会保留内存态，等用户处理后再释放）
  const finishedRunId = run?.finished ? run.assistantMsgId : null;
  useEffect(() => {
    if (!finishedRunId) return;
    let active = true;
    (async () => {
      const list = await loadMessages();
      if (!active) return;
      setDbMessages(list);
      clearFinishedRun(currentSessionId);
    })();
    return () => {
      active = false;
    };
  }, [finishedRunId, currentSessionId, loadMessages]);

  // 数据库列表 + 正在生成的消息覆盖层
  let messages = dbMessages;
  if (run) {
    const live = buildLiveMessage(run);
    const index = dbMessages.findIndex((m) => m.id === run.assistantMsgId);
    messages =
      index < 0
        ? [...dbMessages, live]
        : dbMessages.map((m) => (m.id === run.assistantMsgId ? live : m));
  }

  // 待用户回答的意图提问：必须等本轮 AI 完全响应结束（run.finished）后才拉起，
  // 避免生成过程中就弹窗，也避免用户还没来得及选就被清理掉
  let activeClarification = null;
  let activeClarificationMsgId = null;

  if (run?.finished && run.clarification && !run.clarification.handled) {
    activeClarification = parseClarificationArgs(run.clarification.argsText);
    activeClarificationMsgId = run.assistantMsgId;
  }

  if (!activeClarification && !isRunning) {
    // 回退：刷新页面或切换会话后，从本地记录里恢复仍未回答的提问
    for (let i = dbMessages.length - 1; i >= 0; i -= 1) {
      const item = dbMessages[i];
      if (item.role !== 'assistant') continue;
      const pending = item.metadata?.custom?.clarification;
      if (pending && !pending.handled && !resolvedClarificationIds.has(item.id)) {
        activeClarification = parseClarificationArgs(pending.argsText);
        activeClarificationMsgId = item.id;
      }
      break;
    }
  }

  // 同步给回调读取（回调总是在渲染与 effect 之后触发）
  useEffect(() => {
    activeClarificationMsgIdRef.current = activeClarificationMsgId;
  }, [activeClarificationMsgId]);

  // 收尾当前提问：先同步更新内存态（保证紧接着就能发起下一轮），再异步落库
  const clearPendingClarification = useCallback(() => {
    const messageId = activeClarificationMsgIdRef.current;
    if (!messageId) return false;
    setResolvedClarificationIds((prev) => {
      if (prev.has(messageId)) return prev;
      const next = new Set(prev);
      next.add(messageId);
      return next;
    });
    activeClarificationMsgIdRef.current = null;
    dismissClarification(currentSessionId, messageId);
    return true;
  }, [currentSessionId]);

  // 发送新消息：落库后把生成任务交给运行管理器
  const onNew = useCallback(
    async (message) => {
      if (!currentSessionId || isRunActive(currentSessionId)) return;

      const userText = message.content
        .filter((c) => c.type === 'text')
        .map((c) => c.text)
        .join('\n');

      if (!userText.trim()) return;

      // 用户自己输入新内容发送，说明放弃了本轮提问，先把它收尾
      clearPendingClarification();

      const suffix = Math.random().toString(36).substring(2, 6);
      const userMsgId = `msg_${Date.now()}_${suffix}`;
      const assistantMsgId = `msg_${Date.now()}_${suffix}a`;
      const userCreatedAt = nextTimestamp();
      const assistantCreatedAt = nextTimestamp();

      // 是否是本会话第一轮（用于自动命名），必须在写入消息之前统计
      const priorCount = await db.messages
        .where('sessionId')
        .equals(currentSessionId)
        .count();

      await db.transaction('rw', db.messages, async () => {
        await db.messages.add({
          id: userMsgId,
          sessionId: currentSessionId,
          role: 'user',
          content: userText,
          thinking: null,
          createdAt: userCreatedAt,
        });

        await db.messages.add({
          id: assistantMsgId,
          sessionId: currentSessionId,
          role: 'assistant',
          content: '',
          thinking: '',
          thinkingMs: 0,
          toolCalls: [],
          stopped: false,
          createdAt: assistantCreatedAt,
        });
      });

      if (priorCount === 0) {
        // 先用前20个字作为即时占位标题
        const initialTitle = userText.slice(0, 20) + (userText.length > 20 ? '...' : '');
        await db.sessions.update(currentSessionId, { title: initialTitle, updatedAt: Date.now() });
        notifySessionsChanged();

        // 后台异步请求大模型提取提示词意向生成精简标题
        void (async () => {
          try {
            const res = await fetch('/api/chat/title', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                model: currentModel,
                prompt: userText,
              }),
            });
            if (res.ok) {
              const data = await res.json();
              if (data?.title) {
                await db.sessions.update(currentSessionId, {
                  title: data.title,
                  updatedAt: Date.now(),
                });
                notifySessionsChanged();
              }
            }
          } catch (err) {
            console.warn('自动生成会话标题失败:', err);
          }
        })();
      }

      setDbMessages((prev) => [
        ...prev,
        {
          id: userMsgId,
          role: 'user',
          content: [{ type: 'text', text: userText }],
          createdAt: new Date(userCreatedAt),
        },
      ]);

      startChatRun({
        sessionId: currentSessionId,
        assistantMsgId,
        model: currentModel,
        reasoningEffort: currentReasoningEffort,
      });
    },
    [currentSessionId, currentModel, currentReasoningEffort, nextTimestamp, clearPendingClarification]
  );

  // 重新生成 (Reload)
  const onReload = useCallback(async () => {
    if (!currentSessionId || isRunActive(currentSessionId) || messages.length === 0) return;

    // 重新生成时，本轮提问自然作废
    clearPendingClarification();

    const lastMsg = messages[messages.length - 1];
    if (!lastMsg || lastMsg.role !== 'assistant') return;

    await db.messages.update(lastMsg.id, {
      content: '',
      thinking: '',
      thinkingMs: 0,
      toolCalls: [],
      stopped: false,
    });
    setDbMessages((prev) =>
      prev.map((m) =>
        m.id === lastMsg.id
          ? {
              ...m,
              content: [],
              metadata: { custom: { stopped: false, thinkingMs: 0 } },
            }
          : m
      )
    );

    startChatRun({
      sessionId: currentSessionId,
      assistantMsgId: lastMsg.id,
      model: currentModel,
      reasoningEffort: currentReasoningEffort,
    });
  }, [currentSessionId, currentModel, currentReasoningEffort, messages, clearPendingClarification]);

  const onCancel = useCallback(() => {
    if (currentSessionId) abortChatRun(currentSessionId);
  }, [currentSessionId]);

  // 用户在输入框上方选定/输入后提交：收尾提问，并把答案作为新一轮用户消息发给模型
  const submitClarificationAnswer = useCallback(
    (answerText) => {
      clearPendingClarification();
      onNew({
        content: [{ type: 'text', text: answerText }],
      });
    },
    [clearPendingClarification, onNew]
  );

  const runtime = useExternalStoreRuntime({
    messages,
    isRunning,
    isSendDisabled: isRunning,
    onNew,
    onReload,
    onCancel,
    convertMessage: (m) => ({
      id: m.id,
      role: m.role,
      content: m.content,
      createdAt: m.createdAt,
      status: m.status,
      metadata: m.metadata,
    }),
  });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      {React.isValidElement(children)
        ? React.cloneElement(children, {
            activeClarification,
            onSubmitClarificationAnswer: submitClarificationAnswer,
            onDismissClarification: clearPendingClarification,
          })
        : children}
    </AssistantRuntimeProvider>
  );
}
