import { useCallback, useEffect, useRef, useState } from 'react';
import { Loader2 } from 'lucide-react';

import { ChatContainer } from './components/ChatContainer';
import { ChatProvider } from './components/ChatProvider';
import { ErrorBoundary } from './components/ErrorBoundary';
import { LoginPage } from './components/LoginPage';
import { Sidebar } from './components/Sidebar';
import { AdminConsole } from './components/admin/AdminConsole';
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
 *
 * `refreshModels` 返回的 Promise 在**这次拉取真正结束**时才结算：ModelSelector 的重试
 * 按钮靠它维持转圈态，只 bump token 立刻返回会让转圈只闪一帧（见 ModelSelector 的
 * `onRefreshModels` 契约）。
 */
function useModels() {
  const [state, setState] = useState<ModelsState>({
    models: [],
    modelsError: null,
    defaultModel: null,
  });
  const [reloadToken, setReloadToken] = useState(0);
  /** 等待「本次拉取结束」的调用方。同一个 token 只跑一次请求，因此用队列而不是 Map。 */
  const pendingReloads = useRef<Array<() => void>>([]);

  const settleReloads = useCallback(() => {
    const waiters = pendingReloads.current;
    pendingReloads.current = [];
    for (const resolve of waiters) resolve();
  }, []);

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
      } finally {
        // 只有最新一次拉取才结算：旧请求已被 cleanup 标成过期，它的完成不代表
        // 「最新数据已回来」，提前结算会让转圈在该转时停下
        if (active) settleReloads();
      }
    })();

    return () => {
      active = false;
    };
  }, [reloadToken, settleReloads]);

  const refreshModels = useCallback(
    () =>
      new Promise<void>((resolve) => {
        // 先登记再 bump：effect 在本次提交之后才跑，不存在「请求结束早于登记」的窗口
        pendingReloads.current.push(resolve);
        setReloadToken((token) => token + 1);
      }),
    [],
  );

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
    <div
      role="status"
      className="app-shell flex w-screen items-center justify-center bg-background text-foreground"
    >
      {/* 转圈图标是装饰，读屏要听的是下面这句状态文本 */}
      <Loader2 className="size-5 animate-spin text-muted-foreground" aria-hidden="true" />
      <span className="sr-only">正在检查登录状态…</span>
    </div>
  );
}

export interface AuthenticatedShellProps {
  userEmail: string | null;
  isSuperuser: boolean;
  onOpenAdmin: () => void;
  onLogout: () => void | Promise<unknown>;
}

/** 抽屉根节点与首个可聚焦控件的选择器。Sidebar 的实现由其他 wave 负责，这里只按语义标签定位。 */
const SIDEBAR_DRAWER_SELECTOR = 'aside[aria-label="历史对话侧边栏"]';
const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * 移动端抽屉的键盘/读屏可达性：打开时把焦点移进抽屉、Esc 关闭。
 *
 * 遮罩只挡住了鼠标，`inert` 也只解决背景不可达；焦点若不跟着进抽屉，键盘用户仍会
 * 停在看不见的背景控件上（Tab 一圈都在 inert 之外），读屏也不会知道抽屉出现了。
 */
function useMobileDrawerA11y(isMobile: boolean, isOpen: boolean, onClose: () => void): void {
  useEffect(() => {
    if (!isMobile || !isOpen) return undefined;

    const drawer = document.querySelector<HTMLElement>(SIDEBAR_DRAWER_SELECTOR);
    // preventScroll：抽屉是 fixed 定位，聚焦不该带着页面滚一下
    (drawer?.querySelector<HTMLElement>(FOCUSABLE_SELECTOR) ?? drawer)?.focus({ preventScroll: true });

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      // 抽屉里弹出的确认框是 portal 到 body 的独立弹层：焦点在弹层里时 Esc 只该关它
      const target = event.target;
      if (target instanceof Element && target.closest('[role="dialog"], [role="alertdialog"]')) return;
      onClose();
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [isMobile, isOpen, onClose]);
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
function AuthenticatedShell({ userEmail, isSuperuser, onOpenAdmin, onLogout }: AuthenticatedShellProps) {
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

  // 收敛为稳定引用：内联箭头会在 App 重渲染时换身份，令 SidebarSessionItem 的 memo 失效。
  const handleDeleteSession = useCallback(
    (sessionId: string) => removeSessions([sessionId]),
    [removeSessions]
  );

  const closeSidebar = useCallback(() => setSidebarOverride(false), []);
  useMobileDrawerA11y(isMobile, isSidebarOpen, closeSidebar);

  return (
    <div className="app-shell flex w-screen overflow-hidden bg-background text-foreground font-sans antialiased">
      <Sidebar
        sessions={sessions}
        currentSessionId={currentSessionId}
        onSelectSession={handleSelectSession}
        onCreateSession={handleCreateSession}
        onUpdateTitle={updateTitle}
        onDeleteSession={handleDeleteSession}
        onTogglePinSession={togglePin}
        onBatchDelete={removeSessions}
        onBatchTogglePin={setPinned}
        onClearAllSessions={clearAllSessions}
        isOpen={isSidebarOpen}
        onToggleOpen={handleToggleSidebar}
        userEmail={userEmail}
        isSuperuser={isSuperuser}
        onOpenAdmin={onOpenAdmin}
        onLogout={onLogout}
      />

      {/* 遮罩：仅移动端抽屉需要 */}
      {isMobile && isSidebarOpen && (
        <div
          onClick={() => setIsSidebarOpen(false)}
          aria-hidden="true"
          className="fixed inset-0 bg-black/60 z-20 md:hidden backdrop-blur-xs"
        />
      )}

      {/*
        对话主界面：包裹在 ChatProvider (assistant-ui Runtime) 中，Thread 位于 elements/thread.aui。
        `inert` 在移动端抽屉打开时封住背景（遮挡层只挡指针事件），min-h-0 保证内部
        滚动容器拿得到确定高度。桌面端与抽屉关闭时都不受影响。
      */}
      <main inert={isMobile && isSidebarOpen} className="flex min-h-0 min-w-0 flex-1">
        {currentSessionId && (
          <ErrorBoundary key={currentSessionId} fallbackTitle="对话区域加载异常">
            <ChatProvider
              key={currentSessionId}
              currentSessionId={currentSessionId}
              currentModel={currentModel}
              currentReasoningEffort={currentReasoningEffort}
            >
              <ChatContainer
                sessionTitle={sessions.find((s) => s.id === currentSessionId)?.title}
                onUpdateTitle={(title) => {
                  if (currentSessionId) void updateTitle(currentSessionId, title);
                }}
                models={models}
                modelsError={modelsError}
                currentModel={currentModel}
                currentReasoningEffort={currentReasoningEffort}
                onSelectModel={selectModel}
                onSelectReasoningEffort={selectReasoningEffort}
                onRefreshModels={refreshModels}
                onToggleSidebar={handleToggleSidebar}
                onCreateSession={handleCreateSession}
                isSidebarOpen={isSidebarOpen}
              />
            </ChatProvider>
          </ErrorBoundary>
        )}
      </main>
    </div>
  );
}

/**
 * 应用根：认证网关。
 *
 * 三态分流：探测中显示占位、未登录显示登录页、已登录才挂载主界面。
 * 会话过期的处理不在这里——`apiFetch` 收到 401 会通知 `useAuth`，状态自然回到
 * 未登录，于是本组件重新渲染出登录页。这条链路覆盖所有请求，无需逐处处理。
 *
 * 全局 ErrorBoundary 不在这里：它挂在 main.tsx 里包住 App 本身，否则捕不到 App
 * 自身渲染期抛出的错误。这里只保留「对话区域」那层按会话重建的边界。
 */
export function App() {
  const { status, user, pending, error, login, register, logout, clearError } = useAuth();

  // 管理后台是覆盖层：状态放在 App 才能让 AuthenticatedShell 始终挂载（聊天状态不丢）
  const [adminOpen, setAdminOpen] = useState(false);

  // 会话过期或主动登出后状态回到 anonymous：在渲染期就地复位（React 官方 pattern），
  // 否则标记会留到下一次登录，管理后台一进来就又弹出来。
  if (status !== 'authenticated' && adminOpen) setAdminOpen(false);

  return (
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
        <>
          <AuthenticatedShell
            userEmail={user?.email ?? null}
            isSuperuser={user?.is_superuser ?? false}
            onOpenAdmin={() => setAdminOpen(true)}
            onLogout={logout}
          />
          {adminOpen && (
            <ErrorBoundary fallbackTitle="管理后台加载异常">
              <AdminConsole userEmail={user?.email ?? null} onClose={() => setAdminOpen(false)} />
            </ErrorBoundary>
          )}
        </>
      )}
    </ThemeProvider>
  );
}

export default App;
