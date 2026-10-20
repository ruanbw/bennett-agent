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
 * 过期都会自动退回登录页，不需要每个调用点各写一遍。
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
import { LOCAL_CACHE_UNAVAILABLE_MESSAGE, claimLocalData } from '../lib/local-owner';
import { subscribeAccountSwitched } from '../lib/multi-tab';
import { stopSync } from '../lib/sync/engine';

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
  register: (email: string, password: string) => Promise<RegisterOutcome>;
  logout: () => Promise<void>;
  /**
   * 服务端已让本会话失效（例如刚改完密码）：停同步、退回登录页并说明原因。
   *
   * 与登出的区别是**不清空本地数据**：同一个账号重新登录后历史还在，而登出是
   * 「把设备交出去」的语义。真正的跨账号隔离由 `claimLocalData` 负责。
   */
  expireSession: (message: string) => void;
  clearError: () => void;
}

export function useAuth(): UseAuthResult {
  const [status, setStatus] = useState<AuthStatus>('loading');
  const [user, setUser] = useState<AuthUser | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /**
   * 认领本地缓存，返回「失败原因」或 `null`。
   *
   * 必须在把状态切成已登录（以及启动同步引擎）**之前**完成：本地库可能属于上一个
   * 账号，不先清空的话，新账号会看到别人的会话，同步引擎还会把别人待推送的行借新
   * cookie 推上去。失败时宁可不进主界面，让用户重试（通常是另一个标签页占着库）。
   */
  const claimSession = useCallback(async (userId: string): Promise<string | null> => {
    try {
      await claimLocalData(userId);
      return null;
    } catch (err) {
      console.error('无法确认本地缓存归属:', err);
      return LOCAL_CACHE_UNAVAILABLE_MESSAGE;
    }
  }, []);

  // 启动时探测登录态
  useEffect(() => {
    let active = true;

    void (async () => {
      try {
        const current = await fetchCurrentUser();
        if (!active) return;

        if (!current) {
          setStatus('anonymous');
          return;
        }

        const claimError = await claimSession(current.id);
        if (!active) return;
        if (claimError) {
          setError(claimError);
          setStatus('anonymous');
          return;
        }

        setUser(current);
        setStatus('authenticated');
      } catch (err) {
        if (!active) return;
        console.error('探测登录态失败:', err);
        // 网络故障也当作未登录：让用户看到登录页并有机会重试，比停在转不出去的加载态好。
        // 但必须说明原因——静默当作「未登录」会让用户以为自己的会话丢了。
        setError(describeConnectionError(err));
        setStatus('anonymous');
      }
    })();

    return () => {
      active = false;
    };
  }, [claimSession]);

  // 任何请求拿到 401 都退回登录页
  useEffect(() => {
    const handleUnauthorized = (epoch: number): void => {
      // apiFetch 已按世代过滤过；这里再确认一次，防止那层过滤将来被挪走后，
      // 登录前发出的过期 401 把刚建立的登录态又打回登录页
      if (epoch !== getAuthEpoch()) return;
      // 同步循环必须立刻停：cookie 已经失效，继续重试只会刷日志；而本地待推送的行
      // 要原样留着（同一个人重新登录后还能继续同步，换账号则由 claimLocalData 清空）。
      stopSync();
      setUser(null);
      setStatus('anonymous');
    };

    addUnauthorizedListener(handleUnauthorized);
    // 注销必须传同一个函数引用，否则别的挂载点注册的监听会被一起清掉
    return () => removeUnauthorizedListener(handleUnauthorized);
  }, []);

  /*
    其他标签页换了账号（本地库已被它清空）：本页必须重新探测身份。

    否则本页还以旧账号自居，而 cookie 是共享的——下一个同步请求会拿着新账号的 cookie
    去推旧账号的内容。收到通知先停同步，再按启动流程重走一遍「探测 + 认领」。
  */
  useEffect(() => {
    return subscribeAccountSwitched(() => {
      // 先停同步、退回加载态：加载态会让整个主界面卸载，所有按会话维护的内存状态
      // （会话列表、消息、当前选中）跟着丢掉，重新探测完就必然从库里重读。只换
      // `user` 是不够的——旧的 `useChatSessions` 状态还挂着，会继续显示别人的会话。
      stopSync();
      setUser(null);
      setStatus('loading');
      void (async () => {
        try {
          const current = await fetchCurrentUser();
          if (!current) {
            setUser(null);
            setStatus('anonymous');
            return;
          }
          const claimError = await claimSession(current.id);
          if (claimError) {
            setUser(null);
            setError(claimError);
            setStatus('anonymous');
            return;
          }
          setUser(current);
          setStatus('authenticated');
        } catch (err) {
          console.error('账号切换后重新探测登录态失败:', err);
        }
      })();
    });
  }, [claimSession]);

  /**
   * 登录。pending 必须覆盖整个流程（POST + `/me` 探测）：只包 POST 的话，探测期间提
   * 交按钮会复活，用户连点就会发出第二次登录——后端每次登录都新建一条会话记录。
   */
  const login = useCallback(
    async (email: string, password: string): Promise<boolean> => {
      return withPending(setPending, async () => {
        setError(null);

        const attempt = await loginWithSession(email, password);
        if (attempt.ok) {
          const claimError = await claimSession(attempt.user.id);
          if (claimError) {
            setError(claimError);
            return false;
          }
          setUser(attempt.user);
          setStatus('authenticated');
          return true;
        }

        setError(attempt.message);
        // 凭据错误时用户本来就在登录页；但「POST 成功、会话没建立」时必须明确回到未登录，
        // 不能停在「已登录但不知道是谁」的半残状态
        if (attempt.stage === 'session') setStatus('anonymous');
        return false;
      });
    },
    [claimSession]
  );

  const register = useCallback(
    async (email: string, password: string): Promise<RegisterOutcome> => {
      return withPending(setPending, async () => {
        setError(null);

        const attempt = await registerWithSession(email, password);
        if (attempt.ok) {
          const claimError = await claimSession(attempt.user.id);
          if (claimError) {
            // 账号已经建好了，留在注册模式再点一次只会拿到「该邮箱已注册」。
            // 按「注册成功但自动登录失败」处理：切回登录模式，错误文案说明原因。
            setError(claimError);
            return 'login-failed';
          }
          setUser(attempt.user);
          setStatus('authenticated');
          return 'ok';
        }

        setError(attempt.message);
        if (attempt.outcome === 'login-failed') setStatus('anonymous');
        return attempt.outcome;
      });
    },
    [claimSession]
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

  const expireSession = useCallback((message: string) => {
    // 服务端已经删掉了会话行，同步循环继续跑只会不断 401
    stopSync();
    setUser(null);
    setStatus('anonymous');
    setError(message);
  }, []);

  const clearError = useCallback(() => setError(null), []);

  return { status, user, pending, error, login, register, logout, expireSession, clearError };
}
