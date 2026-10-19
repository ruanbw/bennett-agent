import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react';
import type { PropsWithChildren, ReactNode } from 'react';
import {
  AssistantRuntimeProvider,
  MessageNotSentError,
  useExternalStoreRuntime,
} from '@assistant-ui/react';
import { XIcon } from 'lucide-react';
import { db, deleteMessages } from '../db';
import {
  abortChatRun,
  buildLiveMessage,
  clearFinishedRun,
  dismissClarification,
  getRun,
  getRunVersion,
  isRunActive,
  isRunPersistFailed,
  notifySessionsChanged,
  pendingClarification,
  contextExhausted,
  startChatRun,
  subscribeRun,
  toRuntimeMessage,
} from '../lib/chat-runs';
import { apiFetch } from '../lib/api';
import { countSessionMessages, loadSessionMessages } from '../lib/chat-persistence';
import { FALLBACK_TITLE_CHARS, newMessageId } from '../lib/constants';
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

interface ChatSessionContextValue {
  sessionId: string;
  /** 一次性的用户可见提示（发送未成功等）；null 表示没有。 */
  notice: string | null;
  dismissNotice: () => void;
}

const ChatSessionContext = createContext<ChatSessionContextValue | null>(null);

/**
 * assistant-ui runtime 适配层：把「IndexedDB 里的已完成轮次」与「内存里的生成中轮次」
 * 合成一条消息列表，并把发送/重生成/中止接给 `lib/chat-runs`。
 *
 * 这里的几条不变量都是踩过坑之后立的规矩：
 *   - 只订阅当前会话的 run（O-1）——别的会话流式输出时这里不重渲染；
 *   - 待答提问是推导出来的（R-6），不维护可变标记；
 *   - 会话加载与 run 结束后的回读都带世代守卫：在途的旧快照不许覆盖乐观写入；
 *   - 发送路径上「乐观上屏 + startChatRun」之前不能压任何远端 await——标题生成是一次
 *     额外的模型调用，只能 fire-and-forget；
 *   - 会话级提示以组件形式导出（见 `ChatSessionNotices`）：ChatContainer 的 props 由
 *     App 提供，它拿不到当前会话 id。
 */
export function ChatProvider({
  children,
  currentSessionId,
  currentModel,
  currentReasoningEffort,
}: ChatProviderProps) {
  const [dbMessages, setDbMessages] = useState<RuntimeMessage[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const messageSeqRef = useRef(0);
  /**
   * 回读世代号。
   *
   * 「会话加载」与「run 结束后回读」都是 await 回来整表覆盖 UI 列表；期间的任何乐观
   * 写入（新消息上屏、重新生成清空、失败回滚）都会让在途快照过期。开读前固定世代号，
   * 读回来只认最新一代，过期的直接丢弃。
   */
  const loadGenerationRef = useRef(0);

  // 只订阅当前会话：别的会话的增量不会让这里重渲染
  useSyncExternalStore(
    useCallback(
      (listener: () => void) => subscribeRun(currentSessionId, listener),
      [currentSessionId]
    ),
    useCallback(() => getRunVersion(currentSessionId), [currentSessionId])
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

  /** 乐观写入后调用：让所有在途回读作废，别再拿旧快照覆盖新状态。 */
  const invalidateInFlightLoad = useCallback(() => {
    loadGenerationRef.current += 1;
  }, []);

  /**
   * 数据库 → UI 的回读，带世代守卫；返回这次快照是否被采纳。
   *
   * `isActive` 是各 effect 的卸载标记：卸载后照旧忽略结果。
   */
  const applyDbSnapshot = useCallback(
    async (isActive: () => boolean): Promise<boolean> => {
      const generation = ++loadGenerationRef.current;
      const list = await loadMessages();
      if (!isActive() || generation !== loadGenerationRef.current) return false;
      setDbMessages(list);
      return true;
    },
    [loadMessages]
  );

  useEffect(() => {
    let active = true;
    applyDbSnapshot(() => active).catch((err: unknown) => {
      console.warn('读取会话消息失败:', err);
    });
    return () => {
      active = false;
    };
  }, [applyDbSnapshot]);

  // 生成结束后：把最终内容从数据库读回来，再丢弃内存态
  // （若仍有待答提问或落库失败，clearFinishedRun 会保留内存态）
  const finishedRunId = run?.finished ? run.assistantMsgId : null;
  useEffect(() => {
    if (!finishedRunId) return undefined;
    let active = true;
    const settle = async () => {
      const applied = await applyDbSnapshot(() => active);
      // 快照被更新一代的写入取代时不丢内存态：库里可能是旧内容，界面反而会倒退
      if (applied) clearFinishedRun(currentSessionId);
    };
    settle().catch((err: unknown) => {
      console.warn('生成结束后回读会话消息失败:', err);
    });
    return () => {
      active = false;
    };
  }, [finishedRunId, currentSessionId, applyDbSnapshot]);

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

  // 上下文锁：同样是推导出来的。服务端每轮都会拒绝（它才是闸门），这里只是不让
  // 用户白点一次发送，并给出去处——见 lib/chat-message.ts:contextExhausted。
  const contextLocked = contextExhausted(messages, currentModel);

  const dismissPendingClarification = useCallback(() => {
    if (!activeClarification) return;
    dismissClarification(currentSessionId, activeClarification.messageId);
  }, [activeClarification, currentSessionId]);

  const showNotice = useCallback((text: string) => setNotice(text), []);
  const dismissNotice = useCallback(() => setNotice(null), []);

  /**
   * 发送新消息。
   *
   * 顺序是刻意的：落库 → 乐观上屏 → 启动 run，这三步都不等任何远端请求；标题生成放到
   * 最后且不 await。此前标题请求压在中间，首条消息既不显示也没有「生成中」，首 token
   * 还要多等一整次标题调用的时间；更糟的是那段 await 期间 `isRunActive` 仍是 false，
   * 第二个发送者会抢走 run 槽位，让第一条消息留下永远没有回复的空白气泡。
   */
  const onNew = useCallback(
    async (message: AppendMessageInput) => {
      if (!currentSessionId) return;
      // 已有生成在跑：抛给 composer 恢复草稿，而不是静默丢掉用户刚输入的内容
      if (isRunActive(currentSessionId)) throw new MessageNotSentError('已有回复正在生成');

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

      /** 库里的两条记录与已乐观上屏的用户消息一起清掉，并给出可见提示。 */
      const rollback = async (reason: string): Promise<void> => {
        try {
          await deleteMessages([userMsgId, assistantMsgId]);
        } catch (err) {
          console.error('回滚未发送的消息失败:', err);
        }
        invalidateInFlightLoad();
        setDbMessages((prev) => prev.filter((m) => m.id !== userMsgId));
        showNotice(reason);
      };

      try {
        // 首轮判定与写入放进同一个事务：不留「数完还没写」的窗口，也不多一次往返
        let priorCount = 0;
        await db.transaction('rw', db.messages, async () => {
          priorCount = await countSessionMessages(currentSessionId);

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

        invalidateInFlightLoad();
        setDbMessages((prev) => [
          ...prev,
          {
            id: userMsgId,
            role: 'user',
            content: [{ type: 'text', text: userText }],
            createdAt: new Date(userCreatedAt),
          },
        ]);

        const startedRun = startChatRun({
          sessionId: currentSessionId,
          assistantMsgId,
          model: currentModel,
          reasoningEffort: currentReasoningEffort,
        });
        if (!startedRun) {
          // 并发发送抢走了 run 槽位：必须回滚，否则库里与界面上都会留下一条永远没有
          // 回复的空白助手消息
          await rollback('消息未发送成功：已有回复正在生成，请稍后重试');
          throw new MessageNotSentError('已有回复正在生成');
        }

        setNotice(null);

        if (priorCount === 0) {
          // 标题只是装饰：与消息显示、生成完全解耦，失败也不该打扰用户
          void nameSessionFromFirstMessage(currentSessionId, userText, currentModel).catch(
            (err: unknown) => console.warn('自动生成会话标题失败:', err)
          );
        }
      } catch (err) {
        if (err instanceof MessageNotSentError) throw err;
        console.error('发送消息失败:', err);
        await rollback('消息未发送成功，请重试');
        // 抛给 composer：它会恢复发送时清空的草稿，用户不用重打
        throw new MessageNotSentError('消息未发送成功，请重试');
      }
    },
    [
      currentSessionId,
      currentModel,
      currentReasoningEffort,
      nextTimestamp,
      dismissPendingClarification,
      invalidateInFlightLoad,
      showNotice,
    ]
  );

  /**
   * 重新生成 (Reload)。
   *
   * assistant-ui 传进来的 `parentId` 是**被重新生成那条消息的前一条消息 id**。此前这里
   * 忽略参数、固定取最后一条：在历史回答上点重新生成会静默重跑最后一条——覆盖它的内容，
   * 还再花一次额度。只有定位不到时才退回最后一条助手回答，并在 console 留下说明。
   */
  const onReload = useCallback(
    async (parentId: string | null) => {
      if (!currentSessionId || isRunActive(currentSessionId) || messages.length === 0) return;

      // 重新生成时，本轮提问自然作废
      dismissPendingClarification();

      const target = resolveReloadTarget(messages, parentId);
      if (!target) return;
      if (target.fellBack) {
        console.warn('重新生成：未能按 parentId 定位目标消息，退回最后一条助手回答', parentId);
      }
      if (!target.message.id) return;
      const targetId = target.message.id;

      const startedRun = startChatRun({
        sessionId: currentSessionId,
        assistantMsgId: targetId,
        model: currentModel,
        reasoningEffort: currentReasoningEffort,
      });
      if (!startedRun) {
        // run 槽位被别处抢占：先别清内容，否则这条回答会白丢
        showNotice('重新生成失败：已有回复正在生成，请稍后重试');
        return;
      }
      setNotice(null);

      try {
        await db.messages.update(targetId, {
          content: '',
          thinking: '',
          thinkingMs: 0,
          toolCalls: [],
          stopped: false,
          clarification: null,
          // 重新生成是全新一轮：旧的失败/备用模型事实不能带进来
          fallback: null,
          error: null,
        });
      } catch (err) {
        // 清不掉旧内容不影响本轮生成：内存覆盖层与终态落库都会盖过去
        console.warn('重新生成：清空旧内容失败:', err);
      }

      invalidateInFlightLoad();
      setDbMessages((prev) =>
        prev.map((m) =>
          m.id === targetId
            ? {
                ...m,
                content: [],
                metadata: {
                  custom: { stopped: false, thinkingMs: 0, clarification: null, fallback: null, error: null },
                },
              }
            : m
        )
      );
    },
    [
      currentSessionId,
      currentModel,
      currentReasoningEffort,
      messages,
      dismissPendingClarification,
      invalidateInFlightLoad,
      showNotice,
    ]
  );

  // adapter 声明 onCancel 返回 Promise<void>；实现里是同步 abort，包成 async 只是满足返回类型
  const onCancel = useCallback(async () => {
    if (currentSessionId) abortChatRun(currentSessionId);
  }, [currentSessionId]);

  // 用户在输入框上方选定/输入后提交：收尾提问，并把答案作为新一轮用户消息发给模型
  const submitClarificationAnswer = useCallback(
    (answerText: string) => {
      dismissPendingClarification();
      // 这条路径没有 composer 承接 MessageNotSentError 的草稿恢复；失败提示已由 onNew 给出
      onNew({ content: [{ type: 'text', text: answerText }] }).catch((err: unknown) => {
        console.warn('提交澄清回答失败:', err);
      });
    },
    [dismissPendingClarification, onNew]
  );

  const runtime = useExternalStoreRuntime({
    messages,
    isRunning,
    // 上下文已满时连发送按钮一起禁掉：同一模型下重发必然同样失败，历史只会更长
    isSendDisabled: isRunning || contextLocked,
    onNew,
    onReload,
    onCancel,
    convertMessage,
  });

  const sessionContext = useMemo<ChatSessionContextValue>(
    () => ({ sessionId: currentSessionId, notice, dismissNotice }),
    [currentSessionId, notice, dismissNotice]
  );

  return (
    <ChatSessionContext.Provider value={sessionContext}>
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
    </ChatSessionContext.Provider>
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

/** 重新生成的落点：`fellBack` 表示没能按 parentId 定位，退回了最后一条助手回答。 */
interface ReloadTarget {
  message: RuntimeMessage;
  fellBack: boolean;
}

/**
 * 由 assistant-ui 传来的 `parentId` 定位要重新生成的那条助手消息。
 *
 * `parentId` 是被重生成消息的**前一条**消息 id，所以落点是它后面那条；定位不到时（空、
 * 已删除、后继不是助手消息）退回最后一条助手回答，由调用方记录日志。
 */
function resolveReloadTarget(
  messages: readonly RuntimeMessage[],
  parentId: string | null
): ReloadTarget | null {
  if (parentId) {
    const parentIndex = messages.findIndex((m) => m.id === parentId);
    const following = parentIndex >= 0 ? messages[parentIndex + 1] : undefined;
    if (following?.role === 'assistant') return { message: following, fellBack: false };
  }

  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const candidate = messages[i];
    if (candidate?.role === 'assistant') return { message: candidate, fellBack: true };
  }
  return null;
}

/**
 * 会话级提示条：发送未成功 / 本轮回复未能落库。
 *
 * 消费端做成组件而不是让 ChatContainer 自己查：ChatContainer 的 props 由 App 给出，
 * 拿不到当前会话 id，而「本轮是否落库失败」只有 run store 知道（按 sessionId 查询）。
 * ChatContainer 只需在顶部渲染 `<ChatSessionNotices />`。
 */
export function ChatSessionNotices() {
  const session = useContext(ChatSessionContext);
  if (!session) return null;
  return <SessionNotices {...session} />;
}

const SessionNotices = ({ sessionId, notice, dismissNotice }: ChatSessionContextValue) => {
  // 落库失败时 run 的内存态会被保留（clearFinishedRun 不删），这里订阅同一份状态，
  // 提示条因此跟着 run 的生死出现/消失，不需要额外的跨组件通道
  const persistFailed = useSyncExternalStore(
    useCallback((listener: () => void) => subscribeRun(sessionId, listener), [sessionId]),
    useCallback(() => isRunPersistFailed(sessionId), [sessionId])
  );

  const [banner, setBanner] = useState({ failed: persistFailed, dismissed: false });
  if (banner.failed !== persistFailed) {
    // 落库失败标志重新置位（下一轮又失败）时允许提示条再次出现——
    // 否则用户关过一次就再也看不到数据丢失警告
    setBanner({ failed: persistFailed, dismissed: false });
  }

  const showPersistBanner = persistFailed && !banner.dismissed;
  if (!notice && !showPersistBanner) return null;

  return (
    <div className="flex flex-col gap-2 px-3 pt-3">
      {notice && <NoticeBanner onDismiss={dismissNotice}>{notice}</NoticeBanner>}
      {showPersistBanner && (
        <NoticeBanner onDismiss={() => setBanner({ failed: persistFailed, dismissed: true })}>
          本轮回复未能保存到本地，刷新后可能丢失
        </NoticeBanner>
      )}
    </div>
  );
};

/** 可关闭的提示条。role="alert"：错误提示必须被读屏读出来，不能只靠视觉。 */
const NoticeBanner = ({ children, onDismiss }: PropsWithChildren<{ onDismiss: () => void }>) => (
  <div
    role="alert"
    className="flex items-start justify-between gap-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive"
  >
    <span className="leading-relaxed">{children}</span>
    <button
      type="button"
      onClick={onDismiss}
      aria-label="关闭提示"
      className="shrink-0 cursor-pointer rounded p-0.5 transition hover:bg-destructive/10 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
    >
      <XIcon className="size-3.5" aria-hidden="true" />
    </button>
  </div>
);

/**
 * 首轮对话后自动命名会话。
 *
 * 先用首条消息占位（立即反馈），再后台请模型提炼精简标题。模型调用失败就保留占位标题
 * ——标题只是装饰，不该让用户看到报错。
 *
 * 调用方必须 fire-and-forget：这是一次额外的模型调用 + 限流等待，任何 await 到它都会
 * 拖慢首条消息的显示与生成。
 */
async function nameSessionFromFirstMessage(
  sessionId: string,
  userText: string,
  model: string | null
) {
  const placeholder =
    userText.slice(0, FALLBACK_TITLE_CHARS) +
    (userText.length > FALLBACK_TITLE_CHARS ? '...' : '');
  await db.sessions.update(sessionId, { title: placeholder, updatedAt: Date.now() });
  notifySessionsChanged();

  try {
    const res = await apiFetch('/api/chat/title', {
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
