import { useCallback, useEffect, useRef, useState } from 'react';

import { db } from '../db';
import { DEFAULT_REASONING_EFFORT, newSessionId } from '../lib/constants';
import { reconcileEffort } from '../lib/reasoning-effort';
import { abortChatRun, removeSessionRun, setSessionsChangedHandler } from '../lib/chat-runs';
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
  selectSession: (sessionId: string) => void;
  updateTitle: (sessionId: string, title: string) => Promise<void>;
  togglePin: (sessionId: string) => Promise<void>;
  setPinned: (sessionIds: string[], pinned: boolean) => Promise<void>;
  removeSessions: (sessionIds: string[]) => Promise<void>;
  clearAllSessions: () => Promise<void>;
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

  // 会话列表供回调读取（回调总是晚于渲染触发）
  const sessionsRef = useRef<SessionRecord[]>([]);
  useEffect(() => {
    sessionsRef.current = sessions;
  }, [sessions]);

  // -------------------------------------------------------------------
  // 读取
  // -------------------------------------------------------------------
  /** 加载全部本地会话：置顶在前，再按 updatedAt 倒序。 */
  const reloadSessions = useCallback(async (): Promise<SessionRecord[]> => {
    try {
      const list = await db.sessions.toArray();
      list.sort((a, b) => {
        const pinA = a.isPinned ? 1 : 0;
        const pinB = b.isPinned ? 1 : 0;
        if (pinA !== pinB) return pinB - pinA;
        return (b.updatedAt || 0) - (a.updatedAt || 0);
      });
      setSessions(list);
      return list;
    } catch (err) {
      console.error('加载本地会话失败:', err);
      setSessions([]);
      return [];
    }
  }, []);

  // 后台生成结束时刷新会话列表（即使当前视图在别的会话）
  useEffect(() => {
    setSessionsChangedHandler(reloadSessions);
    return () => setSessionsChangedHandler(null);
  }, [reloadSessions]);

  // -------------------------------------------------------------------
  // 创建 / 切换
  // -------------------------------------------------------------------
  const createSession = useCallback(async (): Promise<string> => {
    const session: SessionRecord = {
      id: newSessionId(),
      title: '新对话',
      model: currentModel,
      reasoningEffort: currentReasoningEffort,
      isPinned: 0,
      createdAt: Date.now(),
      updatedAt: Date.now(),
    };

    await db.sessions.add(session);
    setSessions((prev) => [session, ...prev]);
    setCurrentSessionId(session.id);
    return session.id;
  }, [currentModel, currentReasoningEffort]);

  /** 切换当前会话，并把工具栏对齐到该会话记录的模型/等级。 */
  const selectSession = useCallback(
    (sessionId: string): void => {
      const session = sessionsRef.current.find((s) => s.id === sessionId);
      setCurrentSessionId(sessionId);
      // 会话没存模型时置 null，交给 `currentModel` 派生时回退到默认模型
      setSelectedModel(session?.model ?? null);
      setSelectedEffort(session?.reasoningEffort ?? defaultEffort);
    },
    [defaultEffort],
  );

  const updateTitle = useCallback(
    async (sessionId: string, title: string): Promise<void> => {
      await db.sessions.update(sessionId, { title, updatedAt: Date.now() });
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

      await db.sessions.update(sessionId, {
        isPinned: session.isPinned ? 0 : 1,
        updatedAt: Date.now(),
      });
      await reloadSessions();
    },
    [reloadSessions],
  );

  const setPinned = useCallback(
    async (sessionIds: string[], pinned: boolean): Promise<void> => {
      if (sessionIds.length === 0) return;

      await db.transaction('rw', db.sessions, async () => {
        for (const id of sessionIds) {
          await db.sessions.update(id, { isPinned: pinned ? 1 : 0 });
        }
      });
      await reloadSessions();
    },
    [reloadSessions],
  );

  // -------------------------------------------------------------------
  // 删除：单条、批量、清空共用同一个实现
  // -------------------------------------------------------------------
  const removeSessions = useCallback(
    async (sessionIds: string[]): Promise<void> => {
      if (sessionIds.length === 0) return;

      for (const id of sessionIds) {
        abortChatRun(id);
        removeSessionRun(id);
      }

      await db.transaction('rw', db.sessions, db.messages, async () => {
        for (const id of sessionIds) {
          await db.messages.where('sessionId').equals(id).delete();
          await db.sessions.delete(id);
        }
      });

      const remaining = await reloadSessions();

      // 删掉的不是当前会话时，当前视图不需要动
      if (!sessionIds.includes(currentSessionId ?? '')) return;

      if (remaining.length > 0) selectSession(remaining[0].id);
      else await createSession();
    },
    [currentSessionId, reloadSessions, selectSession, createSession],
  );

  const clearAllSessions = useCallback(async (): Promise<void> => {
    for (const session of sessionsRef.current) {
      abortChatRun(session.id);
      removeSessionRun(session.id);
    }

    await db.transaction('rw', db.sessions, db.messages, async () => {
      await db.messages.clear();
      await db.sessions.clear();
    });

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
      if (list.length === 0) await createSession();
      else selectSession(list[0].id);
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
    selectModel,
    selectReasoningEffort,
    reloadSessions,
  };
}
