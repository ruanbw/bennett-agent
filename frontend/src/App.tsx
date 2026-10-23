import { useCallback, useEffect, useRef, useState } from 'react';
import { Loader2 } from 'lucide-react';

import { ChatContainer } from './components/ChatContainer';
import { ChatProvider } from './components/ChatProvider';
import { ErrorBoundary } from './components/ErrorBoundary';
import { LoginPage } from './components/LoginPage';
import { SettingsDialog } from './components/SettingsDialog';
import { Sidebar } from './components/Sidebar';
import { Toaster } from './components/Toaster';
import { AdminConsole } from './components/admin/AdminConsole';
import { ThemeProvider } from './context/ThemeContext';
import { DetailLevelProvider } from './context/DetailLevelContext';
import { useAuth } from './hooks/use-auth';
import { useCapabilities } from './hooks/use-capabilities';
import { useChatSessions } from './hooks/use-chat-sessions';
import { useForeignFileDropGuard } from './hooks/use-foreign-file-drop-guard';
import { useGlobalShortcuts } from './hooks/use-global-shortcuts';
import { useIsMobileViewport } from './hooks/use-media-query';
import { apiFetch } from './lib/api';
import { canAccessAdmin, isSuperadmin } from './lib/auth';
import { describeConnectionFailure } from './lib/connection-failure';
import { ModelListShapeError, readModelListResponse } from './lib/models-response';
import { downloadMarkdownFile } from './lib/export-markdown';
import { resolveDrawerRestoreTarget, trapTabIndex } from './lib/focus-trap';
import { notify } from './lib/toast';
import { startSync, stopSync } from './lib/sync/engine';
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
 * 按钮靠它维持转圈态，只 bump token 立刻返回会让转圈只闪一帧。
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

        // 外部 JSON 必须先校验再进 state：直接把 `data.models` 塞进去，格式一变
        // （字符串、对象、字段改名）就会在渲染时抛 `models.map is not a function`，
        // 整个应用被 ErrorBoundary 兜住——真正坏的其实只是模型菜单。
        const { models, defaultModel, dropped } = await readModelListResponse(res);
        if (!active) return;

        if (dropped > 0) {
          console.warn(`模型列表里有 ${dropped} 条格式不合法，已忽略`);
        }
        setState({
          models,
          defaultModel,
          modelsError: models.length > 0 ? null : '模型列表为空',
        });
      } catch (err) {
        if (!active) return;
        console.error('获取模型列表失败:', err);
        // 浏览器原始报错（Failed to fetch / HTTP 500）不是给用户看的：翻译成能行动的话。
        // 不认识的异常退化为「请求失败，请稍后重试」，菜单里的「重试」按钮仍然可用。
        // 形状错误单独一句：`describeConnectionFailure` 会把它的内部描述原样透传，
        // 而「models 字段不是数组（收到 string）」不是给用户看的。
        setState((prev) => ({
          ...prev,
          modelsError:
            err instanceof ModelListShapeError
              ? '模型列表返回格式异常，请重试；若一直失败请联系管理员'
              : describeConnectionFailure(err).message,
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

  // 切回这个标签页时重拉一次。模型清单可能在别处被改过（另一个标签页开着管理后台、
  // 或管理员从另一台机器改了启停）：只在挂载时拉一次的话，用户看到的是一份可能已经
  // 过期的列表，而失败的代价是「选了后台已停用的模型 → 请求报 model_unavailable」。
  // 这次拉取不展示 loading：旧列表继续可用，拿到了再替换（失败也保留旧列表）。
  useEffect(() => {
    const handleVisibility = () => {
      if (document.visibilityState === 'visible') void refreshModels();
    };

    document.addEventListener('visibilitychange', handleVisibility);
    return () => document.removeEventListener('visibilitychange', handleVisibility);
  }, [refreshModels]);

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
  /** 当前账号角色（`user` / `admin` / `superadmin`），设置面板里展示。 */
  userRole: string | null;
  /** 服务端已使本会话失效（改密）：切回未登录并说明原因。 */
  onSessionExpired: (message: string) => void;
  canOpenAdmin: boolean;
  /** 是否 superadmin：决定管理后台「用户管理」里能不能改角色。 */
  canManageRoles: boolean;
  onLogout: () => void | Promise<unknown>;
}

/** 抽屉根节点与首个可聚焦控件的选择器。 */
const SIDEBAR_DRAWER_SELECTOR = 'aside[aria-label="历史对话侧边栏"]';
/** 重新展开侧边栏的入口（抽屉收起后出现）：关闭抽屉时把焦点还给这里。 */
const DRAWER_REOPEN_SELECTOR = 'button[aria-label="展开侧边栏"]';
const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * 移动端抽屉的键盘/读屏可达性：打开时把焦点移进抽屉、Tab 在抽屉内循环、Esc 关闭、
 * 关闭后把焦点还给打开它的那个按钮。
 *
 * 遮罩只挡住了鼠标，`inert` 也只解决背景不可达；焦点若不跟着进抽屉，键盘用户仍会
 * 停在看不见的背景控件上，读屏也不会知道抽屉出现了。Tab 不循环的话焦点会跑到地址栏
 * 或背景里被 inert 的控件上，用户以为抽屉坏了。
 */
function useMobileDrawerA11y(isMobile: boolean, isOpen: boolean, onClose: () => void): void {
  useEffect(() => {
    if (!isMobile || !isOpen) return undefined;

    const drawer = document.querySelector<HTMLElement>(SIDEBAR_DRAWER_SELECTOR);
    const previouslyFocused = document.activeElement;
    // 焦点已经在抽屉里就不动它：搜索框的 autoFocus 比本 effect 先执行，胶囊工具栏的
    // 「搜索」就是靠它把光标直接放进入口；无条件聚焦第一个控件会把这个位置抢回去。
    const active = document.activeElement;
    const focusInside = drawer !== null && active !== null && drawer.contains(active);
    if (!focusInside) {
      // preventScroll：抽屉是 fixed 定位，聚焦不该带着页面滚一下
      (drawer?.querySelector<HTMLElement>(FOCUSABLE_SELECTOR) ?? drawer)?.focus({
        preventScroll: true,
      });
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      // 抽屉里弹出的确认框是 portal 到 body 的独立弹层：焦点在弹层里时
      // Esc 只该关它，Tab 也该由弹层自己管（它是模态的）
      const target = event.target;
      if (target instanceof Element && target.closest('[role="dialog"], [role="alertdialog"]')) return;

      if (event.key === 'Escape') {
        onClose();
        return;
      }

      if (event.key !== 'Tab' || !drawer) return;
      const focusable = [...drawer.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)].filter(
        (element) => element.offsetParent !== null || element === document.activeElement,
      );
      const currentIndex = focusable.indexOf(document.activeElement as HTMLElement);
      const next = trapTabIndex(focusable.length, currentIndex, event.shiftKey);
      if (next === null) return;
      event.preventDefault();
      focusable[next]?.focus({ preventScroll: true });
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('keydown', handleKeyDown);
      // 关闭后把焦点还给打开抽屉的按钮：否则焦点落在已被移除的节点上，退回 <body>。
      //
      // 必须延后一拍：cleanup 发生在 React 提交之前，此时「展开侧边栏」按钮还没被重新
      // 渲染出来（打开抽屉时它被卸载了），当场查 DOM 只能拿到 null。
      setTimeout(() => {
        // 这一拍之内抽屉又被打开了（快速连点）：焦点已经在抽屉里，不要再抢
        if (document.querySelector(SIDEBAR_DRAWER_SELECTOR)) return;
        const toRestore = resolveDrawerRestoreTarget(
          previouslyFocused,
          drawer,
          document.querySelector<HTMLElement>(DRAWER_REOPEN_SELECTOR),
        );
        if (toRestore instanceof HTMLElement) toRestore.focus({ preventScroll: true });
      }, 0);
    };
  }, [isMobile, isOpen, onClose]);
}

/**
 * 已登录后的主界面：布局与装配。
 *
 * 单独成组件而不是写在 `App` 里的条件分支中：`useModels` / `useChatSessions` 是
 * 已登录才该跑的副作用，只有整个组件按登录态挂载/卸载才能自然做到，而 hook 不能
 * 条件调用。
 */
function AuthenticatedShell({
  userEmail,
  userRole,
  onSessionExpired,
  canOpenAdmin,
  canManageRoles,
  onLogout,
}: AuthenticatedShellProps) {
  const { models, modelsError, defaultModel, refreshModels } = useModels();
  // 运行时能力只用于裁剪空对话页的起步建议；失败时降级为「无联网搜索」，不阻塞界面
  const { capabilities, failed: capabilitiesFailed, retry: refreshCapabilities } = useCapabilities();
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
    exportSession,
    exportSessions,
    selectModel,
    selectReasoningEffort,
  } = useChatSessions({ models, defaultModel });

  const isMobile = useIsMobileViewport();

  // 管理后台是覆盖层。状态放在 shell 里（而不是 App）：关闭时要能就地刷新模型列表，
  // 而 `/api/models` 的 state 就在这个组件的 `useModels` 里。
  const [adminOpen, setAdminOpen] = useState(false);
  // 设置面板挂在 shell 层：侧边栏收起时 AccountBar 不渲染，齿轮入口在顶栏胶囊工具栏
  const [settingsOpen, setSettingsOpen] = useState(false);

  const handleOpenAdmin = useCallback(() => setAdminOpen(true), []);
  const handleOpenSettings = useCallback(() => setSettingsOpen(true), []);

  const handleCloseAdmin = useCallback(() => {
    setAdminOpen(false);
    // 后台刚改过什么都有可能：启停开关、新增/删除模型、换默认模型。而 `/api/models`
    // 只在挂载与手动重试时拉取，不在这里刷的话聊天框会继续用进后台之前的那份快照
    // （表现为「后台 5 个、聊天框 6 款」，而且停用的模型仍然可选）。
    void refreshModels();
  }, [refreshModels]);

  // 抽屉的开合是「用户手动状态 + 断点默认值」的组合。用渲染期调整而不是 effect：
  // 断点变化时清掉手动覆盖即可，不需要额外一轮渲染。
  const [sidebarOverride, setSidebarOverride] = useState<boolean | null>(null);
  const [trackedIsMobile, setTrackedIsMobile] = useState(isMobile);

  if (trackedIsMobile !== isMobile) {
    setTrackedIsMobile(isMobile);
    setSidebarOverride(null);
  }

  const isSidebarOpen = sidebarOverride ?? !isMobile;
  const setIsSidebarOpen = setSidebarOverride;

  /**
   * 「搜索」请求：收起态没有搜索框，所以由顶栏胶囊工具栏发起「展开侧边栏 + 打开搜索」。
   *
   * 用递增计数器而不是布尔值：布尔值重复置 true 不会触发重渲染，用户关掉搜索后
   * 再点一次就打不开了。计数器的每次变化都是一次独立请求。
   */
  const [searchRequest, setSearchRequest] = useState(0);

  const handleToggleSidebar = useCallback(() => {
    setSidebarOverride(!isSidebarOpen);
  }, [isSidebarOpen]);

  const handleOpenSearch = useCallback(() => {
    // 移动端上「展开侧边栏」就是打开抽屉，搜索框在抽屉里。
    setSidebarOverride(true);
    setSearchRequest((request) => request + 1);
  }, []);

  // ⌘K / Ctrl+K 打开搜索；⌘, / Ctrl+, 打开设置（与胶囊工具栏的两个入口共用动作）
  useGlobalShortcuts({ onOpenSearch: handleOpenSearch, onOpenSettings: handleOpenSettings });

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

  /**
   * 导出整段会话：读库生成 Markdown，再交给浏览器下载。
   *
   * 下载动作留在组件层（`downloadMarkdownFile` 需要 DOM），hook 只负责「记录 → 文本」
   * 这一段纯数据转换——那部分有单测，下载本身没有可断言的状态。
   */
  const handleExportSession = useCallback(
    async (sessionId: string) => {
      const { filename, markdown } = await exportSession(sessionId);
      if (!downloadMarkdownFile(filename, markdown)) {
        // 没有 DOM（SSR/测试环境）时下载根本不会发生：不能报「已导出」
        notify('error', '导出失败：当前环境不支持下载');
        return;
      }
      // 下载动作本身在下载栏里可见，但用户点完菜单后没有任何页面内反馈；给一条确认
      notify('success', `已导出 ${filename}`);
    },
    [exportSession]
  );

  /** 批量导出：合并成一个归档文件（逐个下载会被浏览器拦截并要求多次确认）。 */
  const handleExportSessions = useCallback(
    async (sessionIds: string[]) => {
      const { filename, markdown, sessionCount } = await exportSessions(sessionIds);
      if (!downloadMarkdownFile(filename, markdown)) {
        notify('error', '导出失败：当前环境不支持下载');
        return;
      }
      // 用归档里实际写入的会话数：墓碑会话会被跳过，「选中 N 个」可能大于真实导出数
      const exported = sessionCount ?? sessionIds.length;
      notify('success', `已导出 ${exported} 个会话（${filename}）`);
    },
    [exportSessions]
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
        onExportSession={handleExportSession}
        onExportSessions={handleExportSessions}
        onTogglePinSession={togglePin}
        onBatchDelete={removeSessions}
        onBatchTogglePin={setPinned}
        onClearAllSessions={clearAllSessions}
        isOpen={isSidebarOpen}
        onToggleOpen={handleToggleSidebar}
        searchRequest={searchRequest}
        userEmail={userEmail}
        canOpenAdmin={canOpenAdmin}
        onOpenAdmin={handleOpenAdmin}
        onOpenSettings={handleOpenSettings}
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
        滚动容器拿得到确定高度。
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
                /*
                  必须把 updateTitle 的 Promise 交回去：`void` 掉之后 ChatContainer 的
                  `await onUpdateTitle(...)` 会立刻成功，保存失败既不报错、也不会把编辑器
                  留在原地——用户只看到标题悄悄回退，错误只出现在控制台。
                */
                onUpdateTitle={(title) =>
                  currentSessionId ? updateTitle(currentSessionId, title) : undefined
                }
                models={models}
                modelsError={modelsError}
                webSearchAvailable={capabilities?.web_search ?? false}
                currentModel={currentModel}
                currentReasoningEffort={currentReasoningEffort}
                onSelectModel={selectModel}
                onSelectReasoningEffort={selectReasoningEffort}
                // 模型菜单的「重试」顺带重探运行时能力：管理员刚配好搜索 Key 时，
                // 最常做的就是回聊天页点这个按钮——只刷模型列表会留下「已启用搜索但
                // 起步建议里没有搜索示例」的错位。
                onRefreshModels={async () => {
                  refreshCapabilities();
                  await refreshModels();
                }}
                onToggleSidebar={handleToggleSidebar}
                onOpenSearch={handleOpenSearch}
                onOpenSettings={handleOpenSettings}
                account={{
                  email: userEmail,
                  onLogout,
                  onOpenSettings: handleOpenSettings,
                  canOpenAdmin,
                  onOpenAdmin: handleOpenAdmin,
                }}
                onCreateSession={handleCreateSession}
                isSidebarOpen={isSidebarOpen}
              />
            </ChatProvider>
          </ErrorBoundary>
        )}
      </main>

      <SettingsDialog
        open={settingsOpen}
        onOpenChange={setSettingsOpen}
        userEmail={userEmail}
        userRole={userRole}
        capabilities={capabilities}
        capabilitiesFailed={capabilitiesFailed}
        onRefreshCapabilities={refreshCapabilities}
        onPasswordChanged={() => onSessionExpired('密码已修改，请用新密码重新登录')}
      />

      {adminOpen && (
        <ErrorBoundary fallbackTitle="管理后台加载异常">
          <AdminConsole
            userEmail={userEmail}
            canManageRoles={canManageRoles}
            onClose={handleCloseAdmin}
          />
        </ErrorBoundary>
      )}
    </div>
  );
}

/**
 * 应用根：认证网关。
 *
 * 三态分流：探测中显示占位、未登录显示登录页、已登录才挂载主界面。
 * 会话过期的处理不在这里——`apiFetch` 收到 401 会通知 `useAuth`，状态自然回到未登录。
 *
 * 全局 ErrorBoundary 不在这里：它挂在 main.tsx 里包住 App 本身，否则捕不到 App
 * 自身渲染期抛出的错误。这里只保留「对话区域」那层按会话重建的边界。
 */
export function App() {
  const {
    status,
    user,
    pending,
    error,
    probeFailed,
    retryProbe,
    login,
    register,
    logout,
    expireSession,
    clearError,
  } = useAuth();
  const userId = user?.id ?? null;

  // 把文件拖到输入框以外的地方时，浏览器会导航到那个文件、整个应用被替换掉
  // （正在生成的回答与草稿全丢）。输入框自己处理框内拖放，这里兜住框外。
  useForeignFileDropGuard();

  /*
    同步引擎跟着登录态起停：它持有游标与定时器，必须和「当前是谁」同生命周期。
    依赖写成 userId 而不是 user 对象：`/api/auth/me` 每次刷新都可能返回新对象，
    用对象做依赖会把整条同步链路反复重启。
  */
  useEffect(() => {
    if (!userId) return undefined;
    startSync(userId);
    return () => stopSync();
  }, [userId]);

  return (
    <ThemeProvider>
      {/* 对话显示偏好（详细/简洁）要覆盖登录页之外的整个聊天界面；
          放在这里而不是 AuthenticatedShell 里，是因为设置面板挂在 shell 层。 */}
      <DetailLevelProvider>
      {/* 动作反馈（导出完成 / 新建失败）：持久状态仍用页面内联条，不用 toast */}
      <Toaster />
      {status === 'loading' ? (
        <AuthLoadingScreen />
      ) : status === 'anonymous' ? (
        <LoginPage
          onLogin={login}
          onRegister={register}
          pending={pending}
          error={error}
          onClearError={clearError}
          probeFailed={probeFailed}
          onRetryProbe={() => void retryProbe()}
        />
      ) : (
        /*
          管理后台是 shell 自己的覆盖层：关闭时要刷新模型列表，而模型 state 在 shell 的
          `useModels` 里。登录态一变整个 shell 卸载，后台的开关也跟着自然复位。
        */
        <AuthenticatedShell
          userEmail={user?.email ?? null}
          userRole={user?.role ?? null}
          onSessionExpired={expireSession}
          canOpenAdmin={canAccessAdmin(user)}
          canManageRoles={isSuperadmin(user)}
          onLogout={logout}
        />
      )}
      </DetailLevelProvider>
    </ThemeProvider>
  );
}

export default App;
