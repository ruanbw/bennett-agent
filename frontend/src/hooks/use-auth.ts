/**
 * 登录态。
 *
 * ## 三态而不是两态
 *
 * `loading` 是必须的：应用启动时「是否已登录」要等 `/api/auth/me` 回来才知道。
 * 少了这一态就会先闪一下登录页再跳回主界面。
 *
 * ## 401 的全局处理在这里接线
 *
 * `apiFetch` 发现 401 就通知本 hook 把状态切回未登录，于是**任何**请求触发的会话
 * 过期（过期、被踢、改密后失效）都会自动退回登录页，不需要每个调用点各写一遍。
 * 这也正是当初选服务端会话而非 JWT 换来的能力。
 *
 * ## 具体的流程都在 lib 层
 *
 * 登录（POST + `/me` 探测）、注册（注册 + 自动登录）、登出（中止生成 + 服务端失效 +
 * 清本地库）的时序都放在 `lib/auth.ts`：本仓库的 vitest 跑在 node 环境，没有 DOM 与
 * 渲染器，hook 的分支与时序在那里是测不了的。本 hook 只负责把结果落到 React 状态。
 */

import { useCallback, useEffect, useState } from 'react';

import { addUnauthorizedListener, getAuthEpoch, removeUnauthorizedListener } from '../lib/api';
import {
  LOGOUT_UNCONFIRMED_MESSAGE,
  describeConnectionError,
  fetchCurrentUser,
  loginWithSession,
  logoutAndClearLocalData,
  registerWithSession,
  withPending,
  type AuthUser,
  type LogoutResult,
  type RegisterOutcome,
} from '../lib/auth';

export type AuthStatus = 'loading' | 'authenticated' | 'anonymous';

export interface UseAuthResult {
  status: AuthStatus;
  user: AuthUser | null;
  /** 请求进行中：用于禁用提交按钮，避免重复提交。 */
  pending: boolean;
  error: string | null;
  login: (email: string, password: string) => Promise<boolean>;
  /**
   * 注册并自动登录。返回值可区分「注册失败」与「账号已创建但自动登录失败」，
   * 后者要由表单切回登录模式（见 `LoginPage`）。
   */
  register: (email: string, password: string, inviteCode: string) => Promise<RegisterOutcome>;
  logout: () => Promise<void>;
  clearError: () => void;
}

export function useAuth(): UseAuthResult {
  const [status, setStatus] = useState<AuthStatus>('loading');
  const [user, setUser] = useState<AuthUser | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 启动时探测登录态
  useEffect(() => {
    let active = true;

    void (async () => {
      try {
        const current = await fetchCurrentUser();
        if (!active) return;
        setUser(current);
        setStatus(current ? 'authenticated' : 'anonymous');
      } catch (err) {
        if (!active) return;
        console.error('探测登录态失败:', err);
        // 网络故障也当作未登录：让用户看到登录页并有机会重试，比停在一个转不出去的
        // 加载态要好。但必须说明原因——静默当作「未登录」会让用户以为自己的会话丢了。
        setError(describeConnectionError(err));
        setStatus('anonymous');
      }
    })();

    return () => {
      active = false;
    };
  }, []);

  // 任何请求拿到 401 都退回登录页
  useEffect(() => {
    const handleUnauthorized = (epoch: number): void => {
      // apiFetch 已按世代过滤过；这里再确认一次，防止那层过滤将来被放宽/挪走后，
      // 登录前发出的过期 401 把刚建立的登录态又打回登录页
      if (epoch !== getAuthEpoch()) return;
      setUser(null);
      setStatus('anonymous');
    };

    addUnauthorizedListener(handleUnauthorized);
    // 注销必须传同一个函数引用；此前是「设回 null」的单槽写法，多挂载场景下会连别人的监听一起清掉
    return () => removeUnauthorizedListener(handleUnauthorized);
  }, []);

  /**
   * 登录。
   *
   * pending 必须覆盖整个流程（POST + `/me` 探测）：只包 POST 的话，探测期间提交
   * 按钮会复活，用户连点就会发出第二次登录——后端每次登录都新建一条会话记录。
   */
  const login = useCallback(
    async (email: string, password: string): Promise<boolean> => {
      return withPending(setPending, async () => {
        setError(null);

        const attempt = await loginWithSession(email, password);
        if (attempt.ok) {
          setUser(attempt.user);
          setStatus('authenticated');
          return true;
        }

        setError(attempt.message);
        // 凭据错误时用户本来就在登录页，不必动状态；但「POST 成功、会话没建立」时
        // 必须明确回到未登录，不能停在「已登录但不知道是谁」的半残状态
        if (attempt.stage === 'session') setStatus('anonymous');
        return false;
      });
    },
    []
  );

  const register = useCallback(
    async (email: string, password: string, inviteCode: string): Promise<RegisterOutcome> => {
      return withPending(setPending, async () => {
        setError(null);

        const attempt = await registerWithSession(email, password, inviteCode);
        if (attempt.ok) {
          setUser(attempt.user);
          setStatus('authenticated');
          return 'ok';
        }

        setError(attempt.message);
        if (attempt.outcome === 'login-failed') setStatus('anonymous');
        return attempt.outcome;
      });
    },
    []
  );

  const logout = useCallback(async () => {
    let result: LogoutResult;
    try {
      result = await logoutAndClearLocalData();
    } catch (err) {
      // 编排内部已逐个吞掉异常，这里是兜底：无论如何都要把 React 状态退回未登录
      console.error('登出流程异常:', err);
      result = { serverConfirmed: false, error: LOGOUT_UNCONFIRMED_MESSAGE };
    }

    setUser(null);
    setStatus('anonymous');
    // 服务端没确认登出时必须让用户看见（cookie 还在，刷新就会恢复登录）
    setError(result.error);
  }, []);

  const clearError = useCallback(() => setError(null), []);

  return { status, user, pending, error, login, register, logout, clearError };
}
