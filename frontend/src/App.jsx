import React, { useState, useEffect, useCallback, useRef } from 'react';
import { db } from './db';
import { Sidebar } from './components/Sidebar';
import { ChatContainer } from './components/ChatContainer';
import { ChatProvider } from './components/ChatProvider';
import { abortChatRun, setSessionsChangedHandler } from './lib/chat-runs';

// 侧边栏在移动端是抽屉，在桌面端是常驻栏，收起逻辑需要区分
const isMobileViewport = () =>
  typeof window !== 'undefined' && window.matchMedia('(max-width: 767px)').matches;

export function App() {
  const [sessions, setSessions] = useState([]);
  const [currentSessionId, setCurrentSessionId] = useState(null);
  const [models, setModels] = useState([]);
  const [modelsError, setModelsError] = useState(null);
  const [currentModel, setCurrentModel] = useState(null);
  const [webSearch, setWebSearch] = useState(false);
  const [isSidebarOpen, setIsSidebarOpen] = useState(() => !isMobileViewport());

  const currentModelRef = useRef(null);
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

  // 1. 加载所有本地会话
  const loadSessions = useCallback(async () => {
    const list = await db.sessions.orderBy('updatedAt').reverse().toArray();
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
      webSearch: false,
      createdAt: Date.now(),
      updatedAt: Date.now(),
    };
    await db.sessions.add(newSession);
    setSessions((prev) => [newSession, ...prev]);
    setCurrentSessionId(newSession.id);
    setWebSearch(false);
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
        setWebSearch(Boolean(first.webSearch));
      } else {
        await handleCreateSession();
      }
    })();
  }, [refreshModels, loadSessions, handleCreateSession, applyModel]);

  // 会话切换
  const handleSelectSession = useCallback(
    (sessionId) => {
      setCurrentSessionId(sessionId);
      const target = sessionsRef.current.find((s) => s.id === sessionId);
      if (target?.model) applyModel(target.model);
      setWebSearch(Boolean(target?.webSearch));
      if (isMobileViewport()) setIsSidebarOpen(false);
    },
    [applyModel]
  );

  // 重命名会话
  const handleUpdateTitle = useCallback(
    async (sessionId, newTitle) => {
      await db.sessions.update(sessionId, { title: newTitle, updatedAt: Date.now() });
      await loadSessions();
    },
    [loadSessions]
  );

  // 删除会话
  const handleDeleteSession = useCallback(
    async (sessionId) => {
      // 先中止该会话可能正在进行的生成
      abortChatRun(sessionId);
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
      if (currentSessionId) {
        await db.sessions.update(currentSessionId, { model: modelId, updatedAt: Date.now() });
        setSessions((prev) =>
          prev.map((s) => (s.id === currentSessionId ? { ...s, model: modelId } : s))
        );
      }
    },
    [applyModel, currentSessionId]
  );

  // 切换联网搜索
  const handleToggleWebSearch = useCallback(
    async (checked) => {
      setWebSearch(checked);
      if (currentSessionId) {
        await db.sessions.update(currentSessionId, { webSearch: checked, updatedAt: Date.now() });
        setSessions((prev) =>
          prev.map((s) => (s.id === currentSessionId ? { ...s, webSearch: checked } : s))
        );
      }
    },
    [currentSessionId]
  );

  return (
    <div className="dark flex h-screen w-screen overflow-hidden bg-background text-foreground font-sans antialiased">
      {/* 侧边栏 */}
      <Sidebar
        sessions={sessions}
        currentSessionId={currentSessionId}
        onSelectSession={handleSelectSession}
        onCreateSession={handleCreateSession}
        onUpdateTitle={handleUpdateTitle}
        onDeleteSession={handleDeleteSession}
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
          webSearch={webSearch}
        >
          <ChatContainer
            models={models}
            modelsError={modelsError}
            currentModel={currentModel}
            webSearch={webSearch}
            onSelectModel={handleSelectModel}
            onToggleWebSearch={handleToggleWebSearch}
            onRefreshModels={refreshModels}
            onToggleSidebar={() => setIsSidebarOpen((open) => !open)}
          />
        </ChatProvider>
      )}
    </div>
  );
}

export default App;
