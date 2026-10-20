/**
 * 登录 / 注册页。
 *
 * 同一个表单承担两种模式：登录与注册都只需邮箱 + 密码。注册完全开放，但建出来的
 * 账号一律是普通用户（`role=user`），权限只能由超管在后台授予，注册不是提权路径。
 *
 * 注册后 `useAuth` 会再自动登录一次，但这一步**可能**失败（cookie 没被接受、探测超时）。
 * 那时账号其实已经建好了，留在注册模式再点一次只会拿到「该邮箱已注册」，所以这里
 * 切回登录模式。
 */

import { useCallback, useRef, useState, type FormEvent } from 'react';
import { Eye, EyeOff, Loader2 } from 'lucide-react';

import { MIN_PASSWORD_LENGTH, modeAfterRegister, type RegisterOutcome } from '../lib/auth';
import { ThemeSwitcher } from './ThemeSwitcher';
import { Button } from './ui/button';
import { Input } from './ui/input';

type Mode = 'login' | 'register';

export interface LoginPageProps {
  onLogin: (email: string, password: string) => Promise<boolean>;
  /** 返回值区分「注册失败」与「账号已创建但自动登录失败」（见 `RegisterOutcome`）。 */
  onRegister: (email: string, password: string) => Promise<RegisterOutcome>;
  /** 请求进行中：禁用提交，避免重复提交。 */
  pending: boolean;
  error: string | null;
  onClearError: () => void;
}

export function LoginPage({ onLogin, onRegister, pending, error, onClearError }: LoginPageProps) {
  const [mode, setMode] = useState<Mode>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  // 密码默认隐藏；切换模式时复位（前一屏看过的密码不该在新表单里继续明文显示）
  const [showPassword, setShowPassword] = useState(false);
  const passwordInputRef = useRef<HTMLInputElement>(null);

  const isRegister = mode === 'register';

  // 密码长度本地先拦一道，免得用户要等一次往返才知道太短；真正的校验在服务端。
  const tooShort = isRegister && password.length > 0 && password.length < MIN_PASSWORD_LENGTH;
  const canSubmit = email.trim().length > 0 && password.length > 0 && !tooShort && !pending;

  const handleSubmit = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (!canSubmit) return;

      if (!isRegister) {
        await onLogin(email.trim(), password);
        return;
      }

      const outcome = await onRegister(email.trim(), password);
      // 账号已创建时切回登录模式，让用户直接点登录；保留 useAuth 刚设好的提示，
      // 所以不能走 switchMode（它会清错误）。
      setMode(modeAfterRegister(outcome));
    },
    [canSubmit, email, isRegister, onLogin, onRegister, password],
  );

  const switchMode = useCallback(() => {
    setMode((previous) => (previous === 'login' ? 'register' : 'login'));
    setShowPassword(false);
    // 切换模式后上一条错误（如「邮箱已注册」）不再适用，留着会误导
    onClearError();
  }, [onClearError]);

  const invalid = tooShort || Boolean(error);

  // 高度交给 .app-shell（100dvh）：iOS Safari 的 100vh 是含地址栏的大视口，会把底部内容顶出可视区
  return (
    <div className="app-shell flex w-screen items-center justify-center bg-background px-4 font-sans text-foreground antialiased">
      <div className="absolute top-4 right-4">
        <ThemeSwitcher />
      </div>

      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <h1 className="text-2xl font-semibold tracking-tight">Bennett Agent</h1>
          <p className="mt-1.5 text-sm text-muted-foreground">
            {isRegister ? '创建一个账号' : '登录以继续'}
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <label htmlFor="auth-email" className="text-sm font-medium">
              邮箱
            </label>
            <Input
              id="auth-email"
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
              aria-invalid={invalid || undefined}
            />
          </div>

          <div className="space-y-1.5">
            <label htmlFor="auth-password" className="text-sm font-medium">
              密码
            </label>
            <div className="relative">
              <Input
                ref={passwordInputRef}
                id="auth-password"
                /* 显示/隐藏只改 input 的 type，值本身不动 —— 不做「把明文塞进另一个字段」
                   那种实现，避免密码在 DOM 里多一份副本 */
                type={showPassword ? 'text' : 'password'}
                name="password"
                autoComplete={isRegister ? 'new-password' : 'current-password'}
                required
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                aria-invalid={invalid || undefined}
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
                aria-controls="auth-password"
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
            {isRegister ? (
              <p className={tooShort ? 'text-xs text-destructive' : 'text-xs text-muted-foreground'}>
                至少 {MIN_PASSWORD_LENGTH} 个字符
              </p>
            ) : null}
          </div>

          {isRegister ? (
            <p className="text-xs text-muted-foreground">
              注册后是普通用户。需要管理后台权限请联系超级管理员开通。
            </p>
          ) : null}

          {/* role=alert：错误是异步出现的，屏幕阅读器需要被主动告知 */}
          {error ? (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          ) : null}

          <Button type="submit" size="lg" className="w-full" disabled={!canSubmit}>
            {pending ? <Loader2 className="animate-spin" /> : null}
            {isRegister ? '注册并登录' : '登录'}
          </Button>
        </form>

        <p className="mt-6 text-center text-sm text-muted-foreground">
          {isRegister ? '已有账号？' : '还没有账号？'}
          <button
            type="button"
            onClick={switchMode}
            className="ml-1 text-foreground underline-offset-4 hover:underline"
          >
            {isRegister ? '去登录' : '去注册'}
          </button>
        </p>
      </div>
    </div>
  );
}
