import { useCallback, useEffect, useState } from 'react';
import { Loader2 } from 'lucide-react';

import { ChatContainer } from './components/ChatContainer';
import { ChatProvider } from './components/ChatProvider';
import { ErrorBoundary } from './components/ErrorBoundary';
import { LoginPage } from './components/LoginPage';
import { Sidebar } from './components/Sidebar';
import { ThemeProvider } from './context/ThemeContext';
import { useAuth } from './hooks/use-auth';
import { useChatSessions } from './hooks/use-chat-sessions';
import { useIsMobileViewport } from './hooks/use-media-query';
import { apiFetch } from './lib/api';
import type { ModelItem } from './types';

/** `useModels` 的 state 形状。必须显式声明：否则 `[]`/`null` 会被推成 `never[]` 与字面量 `null`，写入真实数据时报错。 */
interface ModelsState {
  models: ModelItem[];
  modelsError: string | null;
  defaultModel: string | null;
}

/**
 * 拉取可用模型列表。
 *
 * 单独成 hook：与会话状态无关，失败时只要能让用户重试即可。
 * `reloadToken` 让重试复用同一条 effect，而不是把请求逻辑塞进回调再由 effect 调用。
 */
function useModels() {
  const [state, setState] = useState<ModelsState>({
    models: [],
    modelsError: null,
    defaultModel: null,
  });
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    // 忽略过期响应：连点重试时，先发的请求可能后到
    let active = true;

    void (async () => {
      try {
        // 走 apiFetch：会话过期时它会通知全局退回登录页，不必在这里判断 401
        const res = await apiFetch('/api/models');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);

        const data = await res.json();
        if (!active) return;

        const models = data.models ?? [];
        setState({
          models,
          defaultModel: data.default_model ?? null,
          modelsError: models.length > 0 ? null : '模型列表为空',
        });
      } catch (err) {
        if (!active) return;
        console.error('获取模型列表失败:', err);
        // catch 的 err 是 unknown，但这里要保留原写法 `err?.message || 兜底` 的语义，故断言取 message
        setState((prev) => ({
          ...prev,
          modelsError: (err as { message?: string } | null)?.message || '模型列表加载失败',
        }));
      }
    })();

    return () => {
      active = false;
    };
  }, [reloadToken]);

  const refreshModels = useCallback(() => setReloadToken((token) => token + 1), []);

  return { ...state, refreshModels };
}

/**
 * 探测登录态期间的占位。
 *
 * 不能直接渲染主界面再跳转：那会先发起 `/api/models` 等已登录才该发的请求，
 * 拿到 401 再被踢回登录页，用户看到一次无意义的闪烁。
 */
function AuthLoadingScreen() {
  return (
    <div className="flex h-screen w-screen items-center justify-center bg-background text-foreground">
      <Loader2 className="size-5 animate-spin text-muted-foreground" aria-label="加载中" />
    </div>
  );
}

export interface AuthenticatedShellProps {
  userEmail: string | null;
  onLogout: () => void | Promise<unknown>;
}

/**
 * 已登录后的主界面：布局与装配。
 *
 * 状态与操作已下沉——会话状态在 `use-chat-sessions`，模型列表在 `useModels`，
 * 视口在 `use-media-query`。此前这些全在 App 里，组件因此有五个变更理由（R-9/O-7）。
 *
 * 单独成组件而不是写在 `App` 里的条件分支中：`useModels` / `useChatSessions` 是
 * 已登录才该跑的副作用，只有整个组件按登录态挂载/卸载才能自然做到，而 hook 不能
 * 条件调用。
 */
function AuthenticatedShell({ userEmail, onLogout }: AuthenticatedShellProps) {
  const { models, modelsError, defaultModel, refreshModels } = useModels();
  const {
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
  } = useChatSessions({ models, defaultModel });

  const isMobile = useIsMobileViewport();

  // 抽屉的开合是「用户手动状态 + 断点默认值」的组合。
  // 用渲染期调整而不是 effect：断点变化时清掉手动覆盖即可，
  // 不需要额外一轮渲染，也不会在挂载时抢先改一次状态。
  const [sidebarOverride, setSidebarOverride] = useState<boolean | null>(null);
  const [trackedIsMobile, setTrackedIsMobile] = useState(isMobile);

  if (trackedIsMobile !== isMobile) {
    setTrackedIsMobile(isMobile);
    setSidebarOverride(null);
  }

  const isSidebarOpen = sidebarOverride ?? !isMobile;
  const setIsSidebarOpen = setSidebarOverride;

  const handleToggleSidebar = useCallback(() => {
    setSidebarOverride(!isSidebarOpen);
  }, [isSidebarOpen]);

  const handleSelectSession = useCallback(
    (sessionId: string) => {
      selectSession(sessionId);
      if (isMobile) setSidebarOverride(false);
    },
    [selectSession, isMobile]
  );

  const handleCreateSession = useCallback(async () => {
    await createSession();
    if (isMobile) setSidebarOverride(false);
  }, [createSession, isMobile]);

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-background text-foreground font-sans antialiased">
      <Sidebar
        sessions={sessions}
        currentSessionId={currentSessionId}
        onSelectSession={handleSelectSession}
        onCreateSession={handleCreateSession}
        onUpdateTitle={updateTitle}
        onDeleteSession={(sessionId: string) => removeSessions([sessionId])}
        onTogglePinSession={togglePin}
        onBatchDelete={removeSessions}
        onBatchTogglePin={setPinned}
        onClearAllSessions={clearAllSessions}
        isOpen={isSidebarOpen}
        userEmail={userEmail}
        onLogout={onLogout}
      />

      {/* 遮罩：仅移动端抽屉需要 */}
      {isSidebarOpen && (
        <div
          onClick={() => setIsSidebarOpen(false)}
          aria-hidden="true"
          className="fixed inset-0 bg-black/60 z-20 md:hidden backdrop-blur-xs"
        />
      )}

      {/* 对话主界面：包裹在 ChatProvider (assistant-ui Runtime) 中，Thread 位于 elements/thread.aui */}
      {currentSessionId && (
        <ErrorBoundary key={currentSessionId} fallbackTitle="对话区域加载异常">
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
              onSelectModel={selectModel}
              onSelectReasoningEffort={selectReasoningEffort}
              onRefreshModels={refreshModels}
              onToggleSidebar={handleToggleSidebar}
              onCreateSession={handleCreateSession}
            />
          </ChatProvider>
        </ErrorBoundary>
      )}
    </div>
  );
}

/**
 * 应用根：认证网关。
 *
 * 三态分流：探测中显示占位、未登录显示登录页、已登录才挂载主界面。
 * 会话过期的处理不在这里——`apiFetch` 收到 401 会通知 `useAuth`，状态自然回到
 * 未登录，于是本组件重新渲染出登录页。这条链路覆盖所有请求，无需逐处处理。
 */
export function App() {
  const { status, user, pending, error, login, register, logout, clearError } = useAuth();

  return (
    <ErrorBoundary fallbackTitle="应用出现异常">
      <ThemeProvider>
        {status === 'loading' ? (
          <AuthLoadingScreen />
        ) : status === 'anonymous' ? (
          <LoginPage
            onLogin={login}
            onRegister={register}
            pending={pending}
            error={error}
            onClearError={clearError}
          />
        ) : (
          <AuthenticatedShell userEmail={user?.email ?? null} onLogout={logout} />
        )}
      </ThemeProvider>
    </ErrorBoundary>
  );
}

export default App;
