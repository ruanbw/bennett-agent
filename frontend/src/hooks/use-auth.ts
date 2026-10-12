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
 */

import { useCallback, useEffect, useState } from 'react';

import { setUnauthorizedListener } from '../lib/api';
import {
  fetchCurrentUser,
  login as loginRequest,
  logout as logoutRequest,
  register as registerRequest,
  type AuthResult,
  type AuthUser,
} from '../lib/auth';

export type AuthStatus = 'loading' | 'authenticated' | 'anonymous';

export interface UseAuthResult {
  status: AuthStatus;
  user: AuthUser | null;
  /** 请求进行中：用于禁用提交按钮，避免重复提交。 */
  pending: boolean;
  error: string | null;
  login: (email: string, password: string) => Promise<boolean>;
  register: (email: string, password: string) => Promise<boolean>;
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
        // 网络故障也当作未登录：让用户看到登录页并有机会重试，
        // 比停在一个转不出去的加载态要好
        setStatus('anonymous');
      }
    })();

    return () => {
      active = false;
    };
  }, []);

  // 任何请求拿到 401 都退回登录页
  useEffect(() => {
    setUnauthorizedListener(() => {
      setUser(null);
      setStatus('anonymous');
    });

    return () => setUnauthorizedListener(null);
  }, []);

  /** 统一处理 pending / 错误，避免每个动作各写一遍 try-catch-finally。 */
  const run = useCallback(async (action: () => Promise<AuthResult>): Promise<boolean> => {
    setPending(true);
    setError(null);

    try {
      const result = await action();
      if (result.ok) return true;
      setError(result.message);
      return false;
    } catch (err) {
      console.error('认证请求失败:', err);
      setError('网络异常，请稍后重试');
      return false;
    } finally {
      setPending(false);
    }
  }, []);

  const login = useCallback(
    async (email: string, password: string): Promise<boolean> => {
      const ok = await run(() => loginRequest(email, password));
      if (!ok) return false;

      // 登录接口只回 204（凭据就是 cookie），用户信息要再取一次
      try {
        const current = await fetchCurrentUser();
        if (!current) {
          // 登录接口说成功、但探测说未登录：cookie 没被浏览器接受（例如
          // AUTH_COOKIE_SECURE=true 却在 http 下访问）。宁可让用户重试，
          // 也不要进到「已登录但不知道是谁」的半残状态。
          setError('登录未生效，请检查浏览器是否接受 cookie');
          setStatus('anonymous');
          return false;
        }
        setUser(current);
        setStatus('authenticated');
        return true;
      } catch (err) {
        console.error('登录成功但获取用户信息失败:', err);
        setError('登录成功，但获取用户信息失败，请重试');
        setStatus('anonymous');
        return false;
      }
    },
    [run]
  );

  const register = useCallback(
    async (email: string, password: string): Promise<boolean> => {
      const ok = await run(() => registerRequest(email, password));
      if (!ok) return false;

      // 后端注册接口不下发 cookie，紧接着登一次，省掉用户再手填一遍密码
      return login(email, password);
    },
    [run, login]
  );

  const logout = useCallback(async () => {
    try {
      await logoutRequest();
    } catch (err) {
      // 服务端会话可能已经失效，但本地仍要退出——卡在「登出失败」比直接退出更糟
      console.warn('登出请求失败，仍然清掉本地登录态:', err);
    }

    setUser(null);
    setStatus('anonymous');
    setError(null);
  }, []);

  const clearError = useCallback(() => setError(null), []);

  return { status, user, pending, error, login, register, logout, clearError };
}
