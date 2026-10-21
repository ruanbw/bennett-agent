import { useCallback, useEffect, useRef, useState } from 'react';

import { db } from '../db';
import { loadSessionMessages } from '../lib/chat-persistence';
import { DEFAULT_REASONING_EFFORT, newSessionId } from '../lib/constants';
import {
  archiveFileName,
  buildArchiveMarkdown,
  buildConversationMarkdown,
  exportFileName,
  type ArchiveEntry,
  type ExportedConversation,
} from '../lib/export-markdown';
import { reconcileEffort } from '../lib/reasoning-effort';
import { pinChange, sortSessions, titleChange } from '../lib/session-list';
import {
  abortChatRun,
  isRunActive,
  removeSessionRun,
  requestSessionMessagesReload,
  setSessionsChangedHandler,
} from '../lib/chat-runs';
import { SESSIONS_CHANGED_DEBOUNCE_MS, subscribeSessionsChanged } from '../lib/multi-tab';
import { requestPush, subscribeApplied } from '../lib/sync/engine';
import type { ModelItem, SessionRecord } from '../types';

/**
 * 会话状态与会话级操作。
 *
 * 从 App.jsx 抽出：那里同时拥有会话 CRUD、模型拉取、模型/等级选择、侧边栏布局
 * 与会话恢复初始化——五个变更理由挤在一个 340 行的组件里。
 *
 * 本轮修掉的三处重复/不一致（R-9）：
 *   - 「中止 run → 事务删除 → 重载 → 修复当前选中」此前写了三遍（单条、批量、清空）；
 *     现在只有 `removeSessions` 一个实现。
 *   - 删除后挑选新会话并恢复模型/等级的逻辑此前散在四处，且 `updated[0].model`
 *     缺了兜底，而旧的 `applyModel` 对 null 静默 no-op，于是 `model: null` 的会话
 *     会让工具栏停在陈旧模型上且不会自愈。
 *   - 切换模型时的等级对齐此前就地写了一份 `off`/`none` 判定，现在走
 *     `reconcileEffort`（R-7）。
 *
 * R-7 的关键结构选择：模型与等级是**派生**的，不是用 effect 去「修正」的。
 *   - `selectedModel === null` 表示「跟随默认模型」，因此默认模型晚到也不需要 effect；
 *   - `currentReasoningEffort` 始终经过 `reconcileEffort`，于是「发给模型的等级
 *     一定是目标模型声明支持的」这个不变量由构造保证，而不是靠回调记得去修。
 */

export interface UseChatSessionsParams {
  models: ModelItem[];
  defaultModel: string | null;
  defaultEffort?: string;
}

export interface UseChatSessionsResult {
  sessions: SessionRecord[];
  currentSessionId: string | null;
  currentModel: string | null;
  currentReasoningEffort: string;
  createSession: () => Promise<string>;
  selectSession: (sessionId: string, sessionList?: SessionRecord[]) => void;
  updateTitle: (sessionId: string, title: string) => Promise<void>;
  togglePin: (sessionId: string) => Promise<void>;
  setPinned: (sessionIds: string[], pinned: boolean) => Promise<void>;
  removeSessions: (sessionIds: string[]) => Promise<void>;
  clearAllSessions: () => Promise<void>;
  /** 读库生成一份 Markdown 导出（含文件名）。会话不存在时 reject。 */
  exportSession: (sessionId: string) => Promise<ExportedConversation>;
  /** 把选中的多个会话合并成一个归档文件；没有任何有效会话时 reject。 */
  exportSessions: (sessionIds: string[]) => Promise<ExportedConversation>;
  selectModel: (modelId: string) => Promise<void>;
  selectReasoningEffort: (effort: string) => Promise<void>;
  reloadSessions: () => Promise<SessionRecord[]>;
}

/** `selectedModel` 用 `null` 表示「跟随默认模型」，避免把 defaultModel 复制进 state。 */
export function useChatSessions({
  models,
  defaultModel,
  defaultEffort = DEFAULT_REASONING_EFFORT,
}: UseChatSessionsParams): UseChatSessionsResult {
  const [sessions, setSessions] = useState<SessionRecord[]>([]);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [selectedModel, setSelectedModel] = useState<string | null>(null);
  const [selectedEffort, setSelectedEffort] = useState(defaultEffort);

  const findModel = useCallback(
    (modelId: string | null | undefined) => models.find((m) => m.id === modelId),
    [models],
  );

  const currentModel = selectedModel ?? defaultModel ?? null;
  const currentReasoningEffort = reconcileEffort(selectedEffort, findModel(currentModel));

  // 会话列表与当前选中供回调读取（回调总是晚于渲染触发）。
  // currentSessionIdRef 同时是「竞态守卫」的比较基准：它由所有改选中的路径同步更新，
  // 因此 await 前后读到的都是最新值，不依赖 React 的调度时机。
  const sessionsRef = useRef<SessionRecord[]>([]);
  const currentSessionIdRef = useRef<string | null>(null);
  useEffect(() => {
    sessionsRef.current = sessions;
  }, [sessions]);

  // -------------------------------------------------------------------
  // 读取
  // -------------------------------------------------------------------
  /** 加载全部本地会话：置顶在前，再按 updatedAt 倒序。 */
  const reloadSessions = useCallback(async (): Promise<SessionRecord[]> => {
    try {
      // 墓碑（deleted）必须过滤掉：它们是同步用的删除标记，不是会话
      const list = sortSessions((await db.sessions.toArray()).filter((session) => !session.deleted));
      sessionsRef.current = list;
      // 只更新列表，绝不改当前选中：重载可能在用户点选/新建的同一窗口内完成，
      // 选中是用户的决定，重载不得覆盖它。
      setSessions(list);
      return list;
    } catch (err) {
      console.error('加载本地会话失败:', err);
      sessionsRef.current = [];
      setSessions([]);
      return [];
    }
  }, []);

  // 后台生成结束时刷新会话列表（即使当前视图在别的会话）
  useEffect(() => {
    setSessionsChangedHandler(reloadSessions);
    return () => setSessionsChangedHandler(null);
  }, [reloadSessions]);

  /**
   * 其他标签页的改动：列表重读，必要时连当前会话的消息一起重读。
   *
   * 列表重读合并到一个窗口里：对方可能短时间里连着发几条通知（两个会话先后生成结束），
   * 而列表只认「最新一次」的结果，逐条重读等于把整表扫几遍。
   *
   * 消息只在「通知的就是当前会话」且「本页没有在跑的 run」时才重读：本页自己流出的内容
   * 只存在内存里，拿库里的旧快照盖回去会让回复凭空回退；本页 run 的收尾本来就会走同一条
   * 回读，不需要别人代劳。
   */
  const remoteReloadTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    const unsubscribe = subscribeSessionsChanged(({ sessionId }) => {
      if (remoteReloadTimerRef.current === null) {
        remoteReloadTimerRef.current = setTimeout(() => {
          remoteReloadTimerRef.current = null;
          void reloadSessions();
        }, SESSIONS_CHANGED_DEBOUNCE_MS);
      }

      // 选中会话用 ref 读而不是闭包捕获：通知可能在用户切走之后才到达
      if (sessionId !== currentSessionIdRef.current) return;
      if (isRunActive(sessionId)) return;
      void requestSessionMessagesReload(sessionId);
    });

    return () => {
      unsubscribe();
      if (remoteReloadTimerRef.current !== null) {
        clearTimeout(remoteReloadTimerRef.current);
        remoteReloadTimerRef.current = null;
      }
    };
  }, [reloadSessions]);

  // -------------------------------------------------------------------
  // 创建 / 切换
  // -------------------------------------------------------------------
  const createSession = useCallback(async (): Promise<string> => {
    // 创建前的选中值：await 期间用户可能已经点了别的会话
    const selectedBeforeCreate = currentSessionIdRef.current;
    const now = Date.now();
    const session: SessionRecord = {
      id: newSessionId(),
      title: '新对话',
      model: currentModel,
      reasoningEffort: currentReasoningEffort,
      isPinned: 0,
      createdAt: now,
      updatedAt: now,
    };

    await db.sessions.add(session);
    requestPush();
    sessionsRef.current = [session, ...sessionsRef.current];
    setSessions((prev) => [session, ...prev]);
    // 守卫式更新：只有当前选中仍是「创建前」的值才切到新会话。用户在等待写入期间
    // 选了别的会话时不能被抢回来。
    if (currentSessionIdRef.current === selectedBeforeCreate) {
      currentSessionIdRef.current = session.id;
      setCurrentSessionId(session.id);
    }
    return session.id;
  }, [currentModel, currentReasoningEffort]);

  /** 切换当前会话，并把工具栏对齐到该会话记录的模型/等级。 */
  const selectSession = useCallback(
    (sessionId: string, sessionList?: SessionRecord[]): void => {
      const source = sessionList ?? sessionsRef.current;
      const session = source.find((s) => s.id === sessionId);
      currentSessionIdRef.current = sessionId;
      setCurrentSessionId(sessionId);
      // 会话没存模型时置 null，交给 `currentModel` 派生时回退到默认模型
      setSelectedModel(session?.model ?? null);
      setSelectedEffort(session?.reasoningEffort ?? defaultEffort);
    },
    [defaultEffort],
  );

  const updateTitle = useCallback(
    async (sessionId: string, title: string): Promise<void> => {
      const session = sessionsRef.current.find((s) => s.id === sessionId);
      const changes = titleChange(session, title, Date.now());
      // 标题没变就什么都不写：同名保存不该 bump updatedAt 把旧会话顶到最前
      if (!changes) return;
      await db.sessions.update(sessionId, changes);
      requestPush();
      await reloadSessions();
    },
    [reloadSessions],
  );

  // -------------------------------------------------------------------
  // 置顶
  // -------------------------------------------------------------------
  const togglePin = useCallback(
    async (sessionId: string): Promise<void> => {
      const session = sessionsRef.current.find((s) => s.id === sessionId);
      if (!session) return;

      await db.sessions.update(sessionId, pinChange(!session.isPinned));
      requestPush();
      await reloadSessions();
    },
    [reloadSessions],
  );

  const setPinned = useCallback(
    async (sessionIds: string[], pinned: boolean): Promise<void> => {
      if (sessionIds.length === 0) return;

      const changes = pinChange(pinned);
      await db.transaction('rw', db.sessions, async () => {
        for (const id of sessionIds) {
          await db.sessions.update(id, changes);
        }
      });
      requestPush();
      await reloadSessions();
    },
    [reloadSessions],
  );

  // -------------------------------------------------------------------
  // 导出
  // -------------------------------------------------------------------
  /**
   * 读库生成 Markdown。
   *
   * 刻意不读内存里的 run：导出的是**已经落库**的内容，与刷新后看到的一致；流式中的
   * 半截回答也照实导出，并带上「生成未完成」的标注，不会伪装成完整回答。
   */
  const exportSession = useCallback(
    async (sessionId: string): Promise<ExportedConversation> => {
      const [session, messages] = await Promise.all([
        db.sessions.get(sessionId),
        loadSessionMessages(sessionId),
      ]);
      // 墓碑会话（同步用的删除标记）同样不可导出：对用户来说它已经不存在了
      if (!session || session.deleted) throw new Error('会话不存在或已被删除');

      return {
        filename: exportFileName(session.title),
        markdown: buildConversationMarkdown(session, messages),
      };
    },
    [],
  );

  /**
   * 批量导出：一个归档文件，而不是逐个触发下载。
   *
   * 顺序按最近活动倒序（与侧边栏一致），不依赖选中集合的插入顺序——否则同样的选择
   * 每次导出的段落顺序都可能不同。墓碑会话直接跳过（它们对用户已经不存在了）。
   */
  const exportSessions = useCallback(
    async (sessionIds: string[]): Promise<ExportedConversation> => {
      const unique = [...new Set(sessionIds)];
      const loaded = await Promise.all(
        unique.map(async (id): Promise<ArchiveEntry | null> => {
          const [session, messages] = await Promise.all([
            db.sessions.get(id),
            loadSessionMessages(id),
          ]);
          if (!session || session.deleted) return null;
          return { session, messages };
        }),
      );

      const entries = loaded.filter((entry): entry is ArchiveEntry => entry !== null);
      if (entries.length === 0) throw new Error('没有可导出的会话');

      entries.sort((a, b) => (b.session.updatedAt || 0) - (a.session.updatedAt || 0));
      return {
        filename: archiveFileName(),
        markdown: buildArchiveMarkdown(entries),
        sessionCount: entries.length,
      };
    },
    [],
  );

  // -------------------------------------------------------------------
  // 删除：单条、批量、清空共用同一个实现
  // -------------------------------------------------------------------
  const removeSessions = useCallback(
    async (sessionIds: string[]): Promise<void> => {
      if (sessionIds.length === 0) return;

      // 先删库、成功后再中止 run：反过来会在事务失败时留下「会话还在，run 已被中止」
      // 的半状态。删除窗口内在途 run 的落库都走 `db.messages.update`（行已不存在时是
      // no-op，见 chat-persistence），所以晚一步中止不会把内容写回来。
      await db.transaction('rw', db.sessions, db.messages, async () => {
        for (const id of sessionIds) {
          await db.messages.where('sessionId').equals(id).delete();
          // 保留一行墓碑而不是直接 delete：同步层要靠它把「删了」推给服务端和其他设备。
          // 直接删的话服务端永远不知道这个会话该消失，下次拉取还会把它带回来。
          await db.sessions.update(id, { deleted: true, updatedAt: Date.now() });
        }
      });
      requestPush();

      for (const id of sessionIds) {
        abortChatRun(id);
        removeSessionRun(id);
      }

      const remaining = await reloadSessions();

      // 用 ref 而不是闭包里的 currentSessionId 判断：删除期间用户可能已经切走，
      // 那种情况当前视图不需要动。
      const current = currentSessionIdRef.current;
      if (!current || !sessionIds.includes(current)) return;

      if (remaining.length > 0) selectSession(remaining[0].id, remaining);
      else await createSession();
    },
    [reloadSessions, selectSession, createSession],
  );

  /**
   * 同步层拉到了本机没有的内容：重读列表，必要时重读当前会话的消息。
   *
   * 跨标签页走 `subscribeSessionsChanged`；这里是**同标签页**的那条路——BroadcastChannel
   * 不会把消息回传给发送方，所以同步引擎必须单独通知自己所在的页面。
   *
   * 会话在别的设备被删掉时列表里就没有它了：那要像本地删除一样切走，否则用户会停在
   * 一个已经不存在的会话上（发消息会 404）。
   */
  useEffect(() => {
    return subscribeApplied((sessionIds: string[]) => {
      void (async () => {
        const list = await reloadSessions();
        const current = currentSessionIdRef.current;
        if (!current) return;

        if (!list.some((session) => session.id === current)) {
          // 会话在别的设备被删了。这里**不能**因为本页还在生成就原地不动：
          // 该会话的落库从此都是 no-op（行已不存在），页面会永久停在一个不存在的会话上，
          // 继续发消息还会写出孤儿消息。中止本轮、切走，是唯一自洽的选择。
          if (isRunActive(current)) abortChatRun(current);
          if (list.length > 0) selectSession(list[0].id, list);
          else await createSession();
          return;
        }

        if (!sessionIds.includes(current) || isRunActive(current)) return;
        void requestSessionMessagesReload(current);
      })();
    });
  }, [reloadSessions, selectSession, createSession]);

  const clearAllSessions = useCallback(async (): Promise<void> => {
    const removedIds = sessionsRef.current.map((session) => session.id);

    // 同 removeSessions：先删库再中止 run，事务失败时内存里的 run 仍然可用
    await db.transaction('rw', db.sessions, db.messages, async () => {
      await db.messages.clear();
      // 同 removeSessions：会话改成墓碑，让「全部清空」也能同步出去
      for (const id of removedIds) {
        await db.sessions.update(id, { deleted: true, updatedAt: Date.now() });
      }
    });
    requestPush();

    for (const id of removedIds) {
      abortChatRun(id);
      removeSessionRun(id);
    }

    // ref 必须跟着一起清：它被回调读取，留着旧列表会让 createSession 把新会话拼到
    // 一批已删除的记录前面，state 与 ref 从此长期不一致。
    sessionsRef.current = [];
    setSessions([]);
    await createSession();
  }, [createSession]);

  // -------------------------------------------------------------------
  // 模型 / 思考等级
  // -------------------------------------------------------------------
  const selectModel = useCallback(
    async (modelId: string): Promise<void> => {
      // 绝不保留目标模型未声明的等级——那正是「用户选了却请求失败」的来源
      const nextEffort = reconcileEffort(selectedEffort, findModel(modelId));

      setSelectedModel(modelId);
      setSelectedEffort(nextEffort);

      if (!currentSessionId) return;

      await db.sessions.update(currentSessionId, {
        model: modelId,
        reasoningEffort: nextEffort,
        updatedAt: Date.now(),
      });
      requestPush();
      setSessions((prev) =>
        prev.map((session) =>
          session.id === currentSessionId
            ? { ...session, model: modelId, reasoningEffort: nextEffort }
            : session,
        ),
      );
    },
    [findModel, selectedEffort, currentSessionId],
  );

  const selectReasoningEffort = useCallback(
    async (effort: string): Promise<void> => {
      const nextEffort = reconcileEffort(effort, findModel(currentModel));
      setSelectedEffort(nextEffort);

      if (!currentSessionId) return;

      await db.sessions.update(currentSessionId, {
        reasoningEffort: nextEffort,
        updatedAt: Date.now(),
      });
      requestPush();
      setSessions((prev) =>
        prev.map((session) =>
          session.id === currentSessionId ? { ...session, reasoningEffort: nextEffort } : session,
        ),
      );
    },
    [findModel, currentModel, currentSessionId],
  );

  // -------------------------------------------------------------------
  // 初始化：恢复最近会话，没有就建一个（全程只执行一次）
  // -------------------------------------------------------------------
  const initializedRef = useRef(false);
  useEffect(() => {
    if (initializedRef.current) return;
    initializedRef.current = true;

    void (async () => {
      const list = await reloadSessions();
      // 列表加载期间用户可能已经自己选了会话（或点了「新对话」）：这时不能抢选中。
      if (currentSessionIdRef.current !== null) return;
      if (list.length === 0) await createSession();
      else selectSession(list[0].id, list);
    })();
  }, [reloadSessions, createSession, selectSession]);

  return {
    sessions,
    currentSessionId,
    currentModel,
    currentReasoningEffort,
    createSession,
    selectSession,
    updateTitle,
    togglePin,
    setPinned,
    removeSessions,
    clearAllSessions,
    exportSession,
    exportSessions,
    selectModel,
    selectReasoningEffort,
    reloadSessions,
  };
}
