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
  getRun,
  getRunsVersion,
  isRunActive,
  notifySessionsChanged,
  startChatRun,
  subscribeRuns,
} from '../lib/chat-runs';

// 把 IndexedDB 记录转换成 runtime 消息
function toRuntimeMessage(record) {
  const content = [];

  // 工具调用部件
  if (Array.isArray(record.toolCalls)) {
    for (const tc of record.toolCalls) {
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
      },
    },
  };
  if (record.role === 'assistant') {
    message.status = { type: 'complete' };
  }
  return message;
}

// 生成中的消息：内容直接来自内存里的 run，随增量变化
function buildLiveMessage(run) {
  const reasoningDone = Boolean(run.content) || run.finished;
  const content = [];

  // 正在或已执行的工具调用部件
  if (Array.isArray(run.toolCalls)) {
    for (const tc of run.toolCalls) {
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
    metadata: {
      custom: { stopped: run.stopped, thinkingMs: run.thinkingMs },
    },
  };
}

export function ChatProvider({ children, currentSessionId, currentModel }) {
  const [dbMessages, setDbMessages] = useState([]);
  const messageSeqRef = useRef(0);

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

  // 从 IndexedDB 读取当前会话消息（注意：不在这里中止任何请求）
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

  // 生成结束后：把最终内容从数据库读回来，再丢弃内存态（两者同批提交，不会闪）
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

  // 发送新消息：落库后把生成任务交给运行管理器
  const onNew = useCallback(
    async (message) => {
      if (!currentSessionId || isRunActive(currentSessionId)) return;

      const userText = message.content
        .filter((c) => c.type === 'text')
        .map((c) => c.text)
        .join('\n');

      if (!userText.trim()) return;

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

      if (priorCount === 0) {
        const shortTitle = userText.slice(0, 20) + (userText.length > 20 ? '...' : '');
        await db.sessions.update(currentSessionId, { title: shortTitle, updatedAt: Date.now() });
        notifySessionsChanged();
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
      });
    },
    [currentSessionId, currentModel, nextTimestamp]
  );

  // 重新生成 (Reload)
  const onReload = useCallback(async () => {
    if (!currentSessionId || isRunActive(currentSessionId) || messages.length === 0) return;

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
    });
  }, [currentSessionId, currentModel, messages]);

  const onCancel = useCallback(() => {
    if (currentSessionId) abortChatRun(currentSessionId);
  }, [currentSessionId]);

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
      {children}
    </AssistantRuntimeProvider>
  );
}
