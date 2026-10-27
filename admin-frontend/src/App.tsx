/**
 * 应用根：登录态与角色网关 + 路由表。
 *
 * ## 路由结构（标准后台管理系统的形态）
 *
 * `/login` 之外的整棵树都包在 `RequireAdmin` 里（登录守卫），页面由
 * `AdminLayout`（左侧菜单 + 右侧内容）通过 `<Outlet>` 渲染；每个菜单项是一个
 * 真实的 URL 路由（/providers、/users），刷新与直达都不丢状态。
 *
 * ## 四态而不是三态
 *
 * 在用户端三态（loading / anonymous / authenticated）之上多一态 **forbidden**：
 * 会话 cookie 与用户端共享，完全可能出现「已登录但角色不够」。这一态必须显式呈现
 * （「没有权限」页 + 登出入口），不能把普通用户当成未登录——那样他会以为自己密码
 * 输错了，重输多少次都进不来。
 *
 * ## 401 不做全局广播
 *
 * 用户端把「任何请求 401」广播成全局登出，因为它有同步引擎和多标签场景要顾；
 * 管理后台请求面小，401 出现时各面板自己把错误渲染出来（与 403 同一处理路径），
 * 用户重新登录即可。登录态只在启动探测与登录动作两个时机确定。
 */

import { useCallback, useEffect, useState } from 'react';
import { BrowserRouter, Navigate, Outlet, Route, Routes, useLocation } from 'react-router';
import { Loader2, ShieldX } from 'lucide-react';

import { LoginPage } from './components/LoginPage';
import { ThemeProvider } from './context/ThemeContext';
import { Button } from './components/ui/button';
import { AdminLayout } from './layouts/AdminLayout';
import { OverviewPanel } from './components/admin/OverviewPanel';
import { ProvidersPanel } from './components/admin/ProvidersPanel';
import { UsersPanel } from './components/admin/UsersPanel';
import { NotFoundPanel } from './components/admin/NotFoundPanel';
import { QueryProvider } from './lib/query';
import { Toaster } from './components/Toaster';
import {
  canAccessAdmin,
  describeConnectionError,
  fetchCurrentUser,
  isSuperadmin,
  login,
  logout,
  roleLabelOf,
  type AuthUser,
} from './lib/auth';
import { HOME_PATH } from './lib/nav';

type AuthStatus = 'loading' | 'anonymous' | 'authenticated';

/** `fn` 执行期间置 pending（登录/登出按钮据此禁用，避免重复提交）。 */
async function withPending<T>(setPending: (value: boolean) => void, fn: () => Promise<T>): Promise<T> {
  setPending(true);
  try {
    return await fn();
  } finally {
    setPending(false);
  }
}

export function App() {
  const [status, setStatus] = useState<AuthStatus>('loading');
  const [user, setUser] = useState<AuthUser | null>(null);
  const [pending, setPending] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [probeFailed, setProbeFailed] = useState(false);

  /**
   * 探测一次登录态（启动、用户点「重试」共用）。
   *
   * 语义：`/api/auth/me` 明确回 401 才算「真的未登录」；网络问题记成 `probeFailed`——
   * 会话 cookie 很可能还完全有效，让用户重输密码是在拿一次网络抖动换一次重新登录。
   */
  const probeSession = useCallback(async () => {
    try {
      const current = await fetchCurrentUser();
      setProbeFailed(false);
      setError(null);
      if (!current) {
        setUser(null);
        setStatus('anonymous');
        return;
      }
      setUser(current);
      setStatus('authenticated');
    } catch (err) {
      console.error('探测登录态失败:', err);
      setError(describeConnectionError(err));
      setProbeFailed(true);
      setStatus('anonymous');
    }
  }, []);

  // 与整个应用同生命周期，不做「忽略迟到结果」的世代守卫：
  // 迟到的 Promise 解析时组件早已随应用销毁。
  useEffect(() => {
    void (async () => {
      await probeSession();
    })();
  }, [probeSession]);

  /** 用户点「重试」时重新探测：先回到加载态，避免登录页闪一下又跳走。 */
  const retryProbe = useCallback(async () => {
    setError(null);
    setProbeFailed(false);
    setStatus('loading');
    await probeSession();
  }, [probeSession]);

  /** 登录。pending 覆盖 POST + `/me` 探测整个流程，连点不会发出第二次登录。 */
  const handleLogin = useCallback(
    async (email: string, password: string): Promise<boolean> => {
      return withPending(setPending, async () => {
        setError(null);

        const attempt = await login(email, password);
        if (!attempt.ok) {
          setError(attempt.message);
          // 「POST 成功、会话没建立」时必须明确停在未登录，
          // 不能停在「已登录但不知道是谁」的半残状态
          if (attempt.stage === 'session') setStatus('anonymous');
          return false;
        }

        setUser(attempt.user);
        setProbeFailed(false);
        setStatus('authenticated');
        return true;
      });
    },
    [],
  );

  const handleLogout = useCallback(async () => {
    setLoggingOut(true);
    let notice: string | null = null;
    try {
      const result = await logout();
      if (!result.ok) notice = result.message ?? null;
    } catch (err) {
      // logout 内部已重试并兜底，这里是最后防线：无论如何都要退回登录页
      console.error('登出流程异常:', err);
      notice = '登出未确认：服务端会话可能仍有效，请关闭浏览器后重试';
    }
    setUser(null);
    setProbeFailed(false);
    setError(null);
    // 服务端没确认登出时必须让用户看见（cookie 还在，刷新就会恢复登录）
    setLoggingOut(false);
    setStatus('anonymous');
    if (notice) setError(notice);
  }, []);

  return (
    <ThemeProvider>
      <QueryProvider>
        <BrowserRouter>
          <Routes>
            <Route
              path="/login"
              // 已登录时的「离开登录页」由 LoginPage 自己负责（带原路径回跳）：
              // 路由层再放一个 <Navigate> 会在登录成功的那一帧抢先跳走，
              // 把深链回跳吃掉（表现为登录后总落在默认页）。
              element={
                <LoginPage
                  authenticated={status === 'authenticated'}
                  onLogin={handleLogin}
                  pending={pending}
                  error={error}
                  probeFailed={probeFailed}
                  onRetryProbe={() => void retryProbe()}
                />
              }
            />

            <Route
              element={
                <RequireAdmin
                  status={status}
                  user={user}
                  onLogout={handleLogout}
                  loggingOut={loggingOut}
                />
              }
            >
              <Route
                element={
                  <AdminLayout
                    userEmail={user?.email ?? null}
                    roleLabel={roleLabelOf(user)}
                    onLogout={handleLogout}
                    loggingOut={loggingOut}
                  />
                }
              >
                <Route index element={<Navigate to={HOME_PATH} replace />} />
                <Route
                  path="overview"
                  element={<OverviewPanel canManageRoles={isSuperadmin(user)} />}
                />
                <Route path="providers" element={<ProvidersPanel />} />
                <Route
                  path="users"
                  // canManageRoles 决定「用户管理」里能否改角色：admin 能进后台、能封
                  // 普通用户，但改角色是 superadmin 的专属动作（后端同样会拦）
                  element={
                    <UsersPanel
                      currentUserEmail={user?.email ?? null}
                      canManageRoles={isSuperadmin(user)}
                    />
                  }
                />
                {/* 后台里的野路径给一页「找不到」，而不是静默跳回首页：手抄 URL 或
                    旧书签出错时，重定向会让人以为「页面被改名了」，而不是「地址不对」。 */}
                <Route path="*" element={<NotFoundPanel />} />
              </Route>
            </Route>
          </Routes>

          <Toaster />
        </BrowserRouter>
      </QueryProvider>
    </ThemeProvider>
  );
}

/**
 * 登录与角色守卫：包住 `/login` 之外的整棵路由树。
 *
 * 未登录跳 `/login` 并带上原路径（登录成功后回跳）；角色不够停留在显式的
 * 「没有权限」页——不是未登录，不能伪装成需要重新输密码。
 */
function RequireAdmin({
  status,
  user,
  onLogout,
  loggingOut,
}: {
  status: AuthStatus;
  user: AuthUser | null;
  onLogout: () => Promise<void>;
  loggingOut: boolean;
}) {
  const location = useLocation();

  if (status === 'loading') return <LoadingScreen />;
  if (status === 'anonymous') {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  }
  if (!canAccessAdmin(user)) {
    return (
      <ForbiddenScreen userEmail={user?.email ?? null} onLogout={onLogout} pending={loggingOut} />
    );
  }
  return <Outlet />;
}

/** 探测期间的占位：必须与登录页同构（满屏居中），否则探测完成时会跳一下。 */
function LoadingScreen() {
  return (
    <div className="app-shell flex w-screen items-center justify-center bg-background font-sans text-foreground antialiased">
      <Loader2 className="size-6 animate-spin text-muted-foreground" aria-label="加载中" />
    </div>
  );
}

/**
 * 已登录但角色不够。明确说明「账号没问题，是权限不够」，并给登出入口——
 * 管理后台只有这一个账号视角，没有「切回用户端」的路径可提供。
 */
function ForbiddenScreen({
  userEmail,
  onLogout,
  pending,
}: {
  userEmail: string | null;
  onLogout: () => Promise<void>;
  pending: boolean;
}) {
  return (
    <div className="app-shell flex w-screen items-center justify-center bg-background px-4 font-sans text-foreground antialiased">
      <div className="w-full max-w-sm text-center">
        <ShieldX className="mx-auto mb-3 size-12 text-muted-foreground" aria-hidden="true" />
        <h1 className="text-xl font-semibold tracking-tight">没有管理后台权限</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          账号{userEmail ? `（${userEmail}）` : ''}不是管理员或超级管理员。
          <br />
          如需权限，请联系超级管理员在「用户管理」中授予。
        </p>
        <Button
          variant="outline"
          className="mt-6 cursor-pointer"
          onClick={() => void onLogout()}
          disabled={pending}
        >
          {pending ? <Loader2 className="animate-spin" aria-hidden="true" /> : null}
          登出
        </Button>
      </div>
    </div>
  );
}

export default App;
