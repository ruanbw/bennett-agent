import React, { useState, useEffect, useCallback, useRef } from 'react';
import { db } from './db';
import { Sidebar } from './components/Sidebar';
import { ChatContainer } from './components/ChatContainer';
import { ChatProvider } from './components/ChatProvider';
import { abortChatRun, removeSessionRun, setSessionsChangedHandler } from './lib/chat-runs';
import { ThemeProvider } from './context/ThemeContext';

// 侧边栏在移动端是抽屉，在桌面端是常驻栏，收起逻辑需要区分
const isMobileViewport = () =>
  typeof window !== 'undefined' && window.matchMedia('(max-width: 767px)').matches;

export function App() {
  const [sessions, setSessions] = useState([]);
  const [currentSessionId, setCurrentSessionId] = useState(null);
  const [models, setModels] = useState([]);
  const [modelsError, setModelsError] = useState(null);
  const [currentModel, setCurrentModel] = useState(null);
  const [currentReasoningEffort, setCurrentReasoningEffort] = useState('medium');
  const [isSidebarOpen, setIsSidebarOpen] = useState(() => !isMobileViewport());

  const currentModelRef = useRef(null);
  const currentReasoningEffortRef = useRef('medium');
  const sessionsRef = useRef([]);
  const initializedRef = useRef(false);

  useEffect(() => {
    sessionsRef.current = sessions;
  }, [sessions]);

  // 模型同时写入 ref：初始化流程里需要立即读到最新值
  const applyModel = useCallback((modelId) => {
    if (!modelId) return;
    currentModelRef.current = modelId;
    setCurrentModel(modelId);
  }, []);

  const applyReasoningEffort = useCallback((effort) => {
    if (!effort) return;
    currentReasoningEffortRef.current = effort;
    setCurrentReasoningEffort(effort);
  }, []);

  // 1. 加载所有本地会话（置顶排在前面，再按 updatedAt 倒序）
  const loadSessions = useCallback(async () => {
    const list = await db.sessions.toArray();
    list.sort((a, b) => {
      const pinA = a.isPinned ? 1 : 0;
      const pinB = b.isPinned ? 1 : 0;
      if (pinA !== pinB) return pinB - pinA;
      return (b.updatedAt || 0) - (a.updatedAt || 0);
    });
    setSessions(list);
    return list;
  }, []);

  // 后台生成完成时需要刷新会话列表（即使当前视图在别的会话）
  useEffect(() => {
    setSessionsChangedHandler(loadSessions);
    return () => setSessionsChangedHandler(null);
  }, [loadSessions]);

  // 2. 拉取模型列表（失败可重试）
  const refreshModels = useCallback(async () => {
    try {
      const res = await fetch('/api/models');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const list = data.models ?? [];
      setModels(list);
      setModelsError(list.length > 0 ? null : '模型列表为空');
      if (data.default_model && !currentModelRef.current) {
        applyModel(data.default_model);
      }
      return data;
    } catch (err) {
      console.error('获取模型列表失败:', err);
      setModelsError(err?.message || '模型列表加载失败');
      return null;
    }
  }, [applyModel]);

  // 3. 创建新会话（依赖全部走 ref，保证引用稳定，避免触发初始化 effect 重跑）
  const handleCreateSession = useCallback(async () => {
    const newSession = {
      id: 'sess_' + Date.now() + '_' + Math.random().toString(36).substring(2, 6),
      title: '新对话',
      model: currentModelRef.current || null,
      reasoningEffort: currentReasoningEffortRef.current || 'medium',
      isPinned: 0,
      createdAt: Date.now(),
      updatedAt: Date.now(),
    };
    await db.sessions.add(newSession);
    setSessions((prev) => [newSession, ...prev]);
    setCurrentSessionId(newSession.id);
    if (isMobileViewport()) setIsSidebarOpen(false);
    return newSession.id;
  }, []);

  // 4. 初次加载：先取默认模型，再恢复最近会话或新建，全程只执行一次
  useEffect(() => {
    if (initializedRef.current) return;
    initializedRef.current = true;

    (async () => {
      const data = await refreshModels();
      const list = await loadSessions();

      if (list.length > 0) {
        const first = list[0];
        setCurrentSessionId(first.id);
        applyModel(first.model || data?.default_model);
        if (first.reasoningEffort) {
          applyReasoningEffort(first.reasoningEffort);
        }
      } else {
        await handleCreateSession();
      }
    })();
  }, [refreshModels, loadSessions, handleCreateSession, applyModel, applyReasoningEffort]);

  // 会话切换
  const handleSelectSession = useCallback(
    (sessionId) => {
      setCurrentSessionId(sessionId);
      const target = sessionsRef.current.find((s) => s.id === sessionId);
      if (target?.model) applyModel(target.model);
      if (target?.reasoningEffort) {
        applyReasoningEffort(target.reasoningEffort);
      }
      if (isMobileViewport()) setIsSidebarOpen(false);
    },
    [applyModel, applyReasoningEffort]
  );

  // 重命名会话
  const handleUpdateTitle = useCallback(
    async (sessionId, newTitle) => {
      await db.sessions.update(sessionId, { title: newTitle, updatedAt: Date.now() });
      await loadSessions();
    },
    [loadSessions]
  );

  // 置顶 / 取消置顶会话
  const handleTogglePinSession = useCallback(
    async (sessionId) => {
      const session = sessionsRef.current.find((s) => s.id === sessionId);
      if (!session) return;
      const nextPinned = session.isPinned ? 0 : 1;
      await db.sessions.update(sessionId, { isPinned: nextPinned, updatedAt: Date.now() });
      await loadSessions();
    },
    [loadSessions]
  );

  // 批量置顶/取消置顶
  const handleBatchTogglePin = useCallback(
    async (sessionIds, pinState) => {
      await db.transaction('rw', db.sessions, async () => {
        for (const id of sessionIds) {
          await db.sessions.update(id, { isPinned: pinState ? 1 : 0 });
        }
      });
      await loadSessions();
    },
    [loadSessions]
  );

  // 批量删除
  const handleBatchDelete = useCallback(
    async (sessionIds) => {
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
      const updated = await loadSessions();
      if (sessionIds.includes(currentSessionId)) {
        if (updated.length > 0) {
          setCurrentSessionId(updated[0].id);
          applyModel(updated[0].model);
        } else {
          await handleCreateSession();
        }
      }
    },
    [currentSessionId, loadSessions, handleCreateSession, applyModel]
  );

  // 清除所有会话
  const handleClearAllSessions = useCallback(async () => {
    // 中止所有会话
    for (const s of sessionsRef.current) {
      abortChatRun(s.id);
      removeSessionRun(s.id);
    }
    await db.transaction('rw', db.sessions, db.messages, async () => {
      await db.messages.clear();
      await db.sessions.clear();
    });
    setSessions([]);
    await handleCreateSession();
  }, [handleCreateSession]);

  // 删除会话
  const handleDeleteSession = useCallback(
    async (sessionId) => {
      // 先中止该会话可能正在进行的生成并从内存清除
      abortChatRun(sessionId);
      removeSessionRun(sessionId);
      await db.messages.where('sessionId').equals(sessionId).delete();
      await db.sessions.delete(sessionId);
      const updated = await loadSessions();

      if (currentSessionId === sessionId) {
        if (updated.length > 0) {
          setCurrentSessionId(updated[0].id);
          applyModel(updated[0].model);
        } else {
          await handleCreateSession();
        }
      }
    },
    [currentSessionId, loadSessions, handleCreateSession, applyModel]
  );

  // 切换模型
  const handleSelectModel = useCallback(
    async (modelId) => {
      applyModel(modelId);
      // 切换模型时，检查新模型支持的思考等级
      const targetModel = models.find((m) => m.id === modelId);
      let nextEffort = currentReasoningEffortRef.current;
      if (targetModel?.supported_reasoning_levels?.length > 0) {
        const levels = targetModel.supported_reasoning_levels;
        // 如果当前等级新模型不支持，且当前不是 off，则调整为新模型的推荐/第一个支持等级
        if (nextEffort !== 'off' && nextEffort !== 'none' && !levels.includes(nextEffort)) {
          nextEffort = levels.includes('medium') ? 'medium' : levels[0];
          applyReasoningEffort(nextEffort);
        }
      }

      if (currentSessionId) {
        await db.sessions.update(currentSessionId, {
          model: modelId,
          reasoningEffort: nextEffort,
          updatedAt: Date.now(),
        });
        setSessions((prev) =>
          prev.map((s) =>
            s.id === currentSessionId ? { ...s, model: modelId, reasoningEffort: nextEffort } : s
          )
        );
      }
    },
    [applyModel, applyReasoningEffort, models, currentSessionId]
  );

  // 切换思考等级
  const handleSelectReasoningEffort = useCallback(
    async (effort) => {
      applyReasoningEffort(effort);
      if (currentSessionId) {
        await db.sessions.update(currentSessionId, { reasoningEffort: effort, updatedAt: Date.now() });
        setSessions((prev) =>
          prev.map((s) => (s.id === currentSessionId ? { ...s, reasoningEffort: effort } : s))
        );
      }
    },
    [applyReasoningEffort, currentSessionId]
  );

  return (
    <ThemeProvider>
      <div className="flex h-screen w-screen overflow-hidden bg-background text-foreground font-sans antialiased">
        {/* 侧边栏 */}
        <Sidebar
          sessions={sessions}
          currentSessionId={currentSessionId}
          onSelectSession={handleSelectSession}
          onCreateSession={handleCreateSession}
          onUpdateTitle={handleUpdateTitle}
          onDeleteSession={handleDeleteSession}
          onTogglePinSession={handleTogglePinSession}
          onBatchDelete={handleBatchDelete}
          onBatchTogglePin={handleBatchTogglePin}
          onClearAllSessions={handleClearAllSessions}
          isOpen={isSidebarOpen}
        />

        {/* 遮罩：仅移动端抽屉需要 */}
        {isSidebarOpen && (
          <div
            onClick={() => setIsSidebarOpen(false)}
            className="fixed inset-0 bg-black/60 z-20 md:hidden backdrop-blur-xs"
          />
        )}

        {/* 对话主界面：包裹在 ChatProvider (assistant-ui Runtime) 中，Thread 组件位于 elements/thread.aui */}
        {currentSessionId && (
          <ChatProvider
            key={currentSessionId}
            currentSessionId={currentSessionId}
            currentModel={currentModel}
            currentReasoningEffort={currentReasoningEffort}
          >
            <ChatContainer
              models={models}
              modelsError={modelsError}
              currentModel={currentModel}
              currentReasoningEffort={currentReasoningEffort}
              onSelectModel={handleSelectModel}
              onSelectReasoningEffort={handleSelectReasoningEffort}
              onRefreshModels={refreshModels}
              onToggleSidebar={() => setIsSidebarOpen((open) => !open)}
              onCreateSession={handleCreateSession}
            />
          </ChatProvider>
        )}
      </div>
    </ThemeProvider>
  );
}

export default App;
