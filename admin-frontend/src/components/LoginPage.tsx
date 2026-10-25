/**
 * 管理后台登录页。
 *
 * 只有登录一种模式：管理员账号由 `scripts/seed_admin.py` 播种或由超管在后台授予，
 * 后台不提供注册（注册入口开在一个纯管理工具上是攻击面，不是便利）。
 *
 * 登录成功后 `App.tsx` 会再做一次角色检查：普通用户登录成功只会看到「无权限」页，
 * 不会因为「能登录」就进了后台。
 */

import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { useLocation, useNavigate } from 'react-router';
import { Eye, EyeOff, Loader2, RefreshCw, ShieldCheck } from 'lucide-react';

import { Button } from './ui/button';
import { Input } from './ui/input';
import { ThemeSwitcher } from './ThemeSwitcher';

export interface LoginPageProps {
  onLogin: (email: string, password: string) => Promise<boolean>;
  /** 登录态已建立：负责跳离登录页（回跳原路径或默认页）。 */
  authenticated: boolean;
  /** 请求进行中：禁用提交，避免重复提交（每次登录后端都会新建一条会话记录）。 */
  pending: boolean;
  error: string | null;
  /**
   * 停在登录页的原因是「探测登录态失败」（网络），而不是服务端明确说未登录。
   *
   * 这两种情况要分开：真正未登录就让用户输密码；只是没探到时，会话 cookie 很可能还
   * 完全有效（比如刚在用户端登录过），让用户重输密码是在拿一次网络抖动换一次重新登录。
   */
  probeFailed?: boolean;
  /** 重新探测登录态；缺省时不显示「重试」。 */
  onRetryProbe?: () => void;
}

export function LoginPage({
  onLogin,
  authenticated,
  pending,
  error,
  probeFailed = false,
  onRetryProbe,
}: LoginPageProps) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const passwordInputRef = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();
  const location = useLocation();

  // 被守卫拦下时的原路径（深链直达的场景），登录成功后回跳
  const state = location.state as { from?: unknown } | null;
  const from =
    typeof state?.from === 'string' && state.from.startsWith('/') ? state.from : '/';

  // 「已登录却还在 /login」只有两种来源：登录刚成功（回跳原路径）、
  // 已登录用户手动访问 /login（回默认页）。跳转只由这里发起，别处不得抢。
  useEffect(() => {
    if (authenticated) navigate(from, { replace: true });
  }, [authenticated, from, navigate]);

  const canSubmit = email.trim().length > 0 && password.length > 0 && !pending;

  const handleSubmit = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (!canSubmit) return;
      await onLogin(email.trim(), password);
      // 跳转在 authenticated 变为 true 的 effect 里发生，这里不重复导航
    },
    [canSubmit, email, onLogin, password],
  );

  // 高度交给 .app-shell（100dvh）：iOS Safari 的 100vh 是含地址栏的大视口，会把底部内容顶出可视区
  return (
    <div className="app-shell flex w-screen items-center justify-center bg-background px-4 font-sans text-foreground antialiased">
      <div className="absolute top-4 right-4">
        <ThemeSwitcher />
      </div>

      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <ShieldCheck className="mx-auto mb-3 size-12 text-primary" aria-hidden="true" />
          <h1 className="text-2xl font-semibold tracking-tight">Bennett 管理后台</h1>
          <p className="mt-1.5 text-sm text-muted-foreground">管理员登录</p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <label htmlFor="admin-email" className="text-sm font-medium">
              邮箱
            </label>
            <Input
              id="admin-email"
              type="email"
              name="email"
              autoComplete="username"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
              autoFocus
              required
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          </div>

          <div className="space-y-1.5">
            <label htmlFor="admin-password" className="text-sm font-medium">
              密码
            </label>
            <div className="relative">
              <Input
                ref={passwordInputRef}
                id="admin-password"
                /* 显示/隐藏只改 input 的 type，值本身不动 —— 不做「把明文塞进另一个字段」
                   那种实现，避免密码在 DOM 里多一份副本 */
                type={showPassword ? 'text' : 'password'}
                name="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                className="pr-10"
              />
              <button
                type="button"
                onClick={() => {
                  setShowPassword((visible) => !visible);
                  // 点击按钮会让输入框失焦；切完把焦点还回去，用户可以直接继续输入
                  passwordInputRef.current?.focus();
                }}
                aria-label={showPassword ? '隐藏密码' : '显示密码'}
                aria-pressed={showPassword}
                aria-controls="admin-password"
                title={showPassword ? '隐藏密码' : '显示密码'}
                className="absolute right-1.5 top-1/2 -translate-y-1/2 flex size-7 items-center justify-center rounded-md text-muted-foreground transition hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60 cursor-pointer"
              >
                {showPassword ? (
                  <EyeOff className="size-4" aria-hidden="true" />
                ) : (
                  <Eye className="size-4" aria-hidden="true" />
                )}
              </button>
            </div>
          </div>

          {/* role=alert：错误是异步出现的，屏幕阅读器需要被主动告知 */}
          {error ? (
            <div id="admin-error" role="alert" className="space-y-1.5">
              <p className="text-sm text-destructive">{error}</p>
              {/* 只是没探到会话时，重输密码是多余的：会话 cookie 可能完全有效 */}
              {probeFailed && onRetryProbe ? (
                // 用带边框的按钮而不是一行下划线小字：在这种状态下它是**主行动**
                // （用户不该为了一个网络抖动重新输密码），埋在提示文字里会被忽略。
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={onRetryProbe}
                  disabled={pending}
                  className="cursor-pointer gap-1.5"
                >
                  <RefreshCw className="size-3.5" aria-hidden="true" />
                  重试检查登录状态
                </Button>
              ) : null}
            </div>
          ) : null}

          <Button type="submit" size="lg" className="w-full" disabled={!canSubmit}>
            {pending ? <Loader2 className="animate-spin" /> : null}
            登录
          </Button>
        </form>
      </div>
    </div>
  );
}
