import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import type { ReactNode } from 'react';
import { AssistantRuntimeProvider, useExternalStoreRuntime } from '@assistant-ui/react';
import { db } from '../db';
import {
  abortChatRun,
  buildLiveMessage,
  clearFinishedRun,
  dismissClarification,
  getRun,
  getRunVersion,
  isRunActive,
  notifySessionsChanged,
  pendingClarification,
  startChatRun,
  subscribeRun,
  toRuntimeMessage,
} from '../lib/chat-runs';
import { countSessionMessages, loadSessionMessages } from '../lib/chat-persistence';
import { API_BASE_URL, FALLBACK_TITLE_CHARS, newMessageId } from '../lib/constants';
import { ClarificationProvider } from '../context/clarification-context';
import type { RuntimeMessage } from '../types';

export interface ChatProviderProps {
  children: ReactNode;
  currentSessionId: string;
  currentModel: string | null;
  currentReasoningEffort: string;
}

/**
 * `onNew` 只读取消息的内容部件。assistant-ui 的 `AppendMessage` 是 system/user/assistant
 * 三种形状的联合，`content` 成员类型随之退化成 `string | 部件数组` 的混合，无法直接收窄；
 * 这里按实际用到的契约声明局部结构类型，调用处结构赋值即可通过。
 */
interface AppendMessageInput {
  content: ReadonlyArray<{ type: string; text?: string }>;
}

/**
 * assistant-ui runtime 适配层：把「IndexedDB 里的已完成轮次」与「内存里的生成中轮次」
 * 合成一条消息列表，并把发送/重生成/中止接给 `lib/chat-runs`。
 *
 * 本轮的两处结构性改动：
 *   - 只订阅当前会话的 run（O-1）。此前订阅一个全局 version，别的会话流式输出时
 *     这里也会重渲染。
 *   - 待答提问改为推导（R-6）。此前要维护 `handled` 标记、`resolvedClarificationIds`
 *     集合与刷新恢复循环，三者都在补偿「状态没有推导来源」。
 */
export function ChatProvider({
  children,
  currentSessionId,
  currentModel,
  currentReasoningEffort,
}: ChatProviderProps) {
  const [dbMessages, setDbMessages] = useState<RuntimeMessage[]>([]);
  const messageSeqRef = useRef(0);

  // 只订阅当前会话：别的会话的增量不会让这里重渲染
  useSyncExternalStore(
    useCallback(
      (listener: () => void) => subscribeRun(currentSessionId, listener),
      [currentSessionId],
    ),
    useCallback(() => getRunVersion(currentSessionId), [currentSessionId]),
  );

  const run = getRun(currentSessionId);
  const isRunning = Boolean(run && !run.finished);

  // 单调递增的时间戳：同一毫秒内连续写入也不会让消息错序
  const nextTimestamp = useCallback(() => {
    const now = Date.now();
    messageSeqRef.current = now <= messageSeqRef.current ? messageSeqRef.current + 1 : now;
    return messageSeqRef.current;
  }, []);

  const loadMessages = useCallback(async () => {
    const records = await loadSessionMessages(currentSessionId);
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
  // （若仍有待答提问，clearFinishedRun 会保留内存态）
  const finishedRunId = run?.finished ? run.assistantMsgId : null;
  useEffect(() => {
    if (!finishedRunId) return undefined;
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

  // 待答提问：从消息列表推导，不看任何可变标记
  const activeClarification = pendingClarification(messages, isRunning);

  const dismissPendingClarification = useCallback(() => {
    if (!activeClarification) return;
    dismissClarification(currentSessionId, activeClarification.messageId);
  }, [activeClarification, currentSessionId]);

  // 发送新消息：落库后把生成任务交给运行管理器
  const onNew = useCallback(
    async (message: AppendMessageInput) => {
      if (!currentSessionId || isRunActive(currentSessionId)) return;

      const userText = message.content
        .filter((c) => c.type === 'text')
        .map((c) => c.text)
        .join('\n');

      if (!userText.trim()) return;

      // 用户自己输入新内容发送，说明放弃了本轮提问
      dismissPendingClarification();

      const userMsgId = newMessageId();
      const assistantMsgId = newMessageId();
      const userCreatedAt = nextTimestamp();
      const assistantCreatedAt = nextTimestamp();

      // 是否是本会话第一轮（用于自动命名），必须在写入消息之前统计
      const priorCount = await countSessionMessages(currentSessionId);

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
          clarification: null,
          createdAt: assistantCreatedAt,
        });
      });

      if (priorCount === 0) {
        await nameSessionFromFirstMessage(currentSessionId, userText, currentModel);
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
    [
      currentSessionId,
      currentModel,
      currentReasoningEffort,
      nextTimestamp,
      dismissPendingClarification,
    ]
  );

  // 重新生成 (Reload)
  const onReload = useCallback(async () => {
    if (!currentSessionId || isRunActive(currentSessionId) || messages.length === 0) return;

    // 重新生成时，本轮提问自然作废
    dismissPendingClarification();

    const lastMsg = messages[messages.length - 1];
    // id 由本层生成、必然存在；ThreadMessageLike 只是把它标成可选，
    // 因此在这里真正收窄，而不是用 `!` 跳过检查
    if (!lastMsg || lastMsg.role !== 'assistant' || !lastMsg.id) return;

    const lastMsgId = lastMsg.id;

    await db.messages.update(lastMsgId, {
      content: '',
      thinking: '',
      thinkingMs: 0,
      toolCalls: [],
      stopped: false,
      clarification: null,
    });
    setDbMessages((prev) =>
      prev.map((m) =>
        m.id === lastMsgId
          ? {
              ...m,
              content: [],
              metadata: { custom: { stopped: false, thinkingMs: 0, clarification: null } },
            }
          : m
      )
    );

    startChatRun({
      sessionId: currentSessionId,
      assistantMsgId: lastMsgId,
      model: currentModel,
      reasoningEffort: currentReasoningEffort,
    });
  }, [
    currentSessionId,
    currentModel,
    currentReasoningEffort,
    messages,
    dismissPendingClarification,
  ]);

  // adapter 声明 onCancel 返回 Promise<void>；实现里是同步 abort，包成 async 只是满足返回类型
  const onCancel = useCallback(async () => {
    if (currentSessionId) abortChatRun(currentSessionId);
  }, [currentSessionId]);

  // 用户在输入框上方选定/输入后提交：收尾提问，并把答案作为新一轮用户消息发给模型
  const submitClarificationAnswer = useCallback(
    (answerText: string) => {
      dismissPendingClarification();
      onNew({ content: [{ type: 'text', text: answerText }] });
    },
    [dismissPendingClarification, onNew]
  );

  const runtime = useExternalStoreRuntime({
    messages,
    isRunning,
    isSendDisabled: isRunning,
    onNew,
    onReload,
    onCancel,
    convertMessage,
  });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ClarificationProvider
        value={{
          pending: activeClarification,
          answer: submitClarificationAnswer,
          dismiss: dismissPendingClarification,
        }}
      >
        {children}
      </ClarificationProvider>
    </AssistantRuntimeProvider>
  );
}

/** 提取到组件外部的稳定转换器，避免重渲染破坏 @assistant-ui 的转换器内部缓存。 */
function convertMessage(m: RuntimeMessage) {
  return {
    id: m.id,
    role: m.role,
    content: m.content,
    createdAt: m.createdAt,
    status: m.status,
    metadata: m.metadata,
  };
}

/**
 * 首轮对话后自动命名会话。
 *
 * 先用首条消息占位（立即反馈），再后台请模型提炼精简标题。模型调用失败就保留占位标题
 * ——标题只是装饰，不该让用户看到报错。
 */
async function nameSessionFromFirstMessage(
  sessionId: string,
  userText: string,
  model: string | null,
) {
  const placeholder = userText.slice(0, FALLBACK_TITLE_CHARS) + (userText.length > FALLBACK_TITLE_CHARS ? '...' : '');
  await db.sessions.update(sessionId, { title: placeholder, updatedAt: Date.now() });
  notifySessionsChanged();

  try {
    const res = await fetch(`${API_BASE_URL}/api/chat/title`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model, prompt: userText }),
    });
    if (!res.ok) return;

    const data = await res.json();
    if (!data?.title) return;

    await db.sessions.update(sessionId, { title: data.title, updatedAt: Date.now() });
    notifySessionsChanged();
  } catch (err) {
    console.warn('自动生成会话标题失败，保留占位标题:', err);
  }
}
