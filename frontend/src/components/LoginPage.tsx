/**
 * 登录 / 注册页。
 *
 * 同一个表单承担两种模式：字段完全相同（邮箱 + 密码），差别只在提交动作与文案，
 * 拆成两个组件会带来两份重复的输入处理与错误展示。
 *
 * 注册成功后由 `useAuth` 自动登录——后端注册接口只返回用户信息、不下发 cookie，
 * 让用户注册完再手填一遍密码是纯粹的摩擦。
 */

import { useCallback, useState, type FormEvent } from 'react';
import { Loader2 } from 'lucide-react';

import { MIN_PASSWORD_LENGTH } from '../lib/auth';
import { ThemeSwitcher } from './ThemeSwitcher';
import { Button } from './ui/button';
import { Input } from './ui/input';

type Mode = 'login' | 'register';

export interface LoginPageProps {
  onLogin: (email: string, password: string) => Promise<boolean>;
  onRegister: (email: string, password: string) => Promise<boolean>;
  /** 请求进行中：禁用提交，避免重复提交。 */
  pending: boolean;
  error: string | null;
  onClearError: () => void;
}

export function LoginPage({ onLogin, onRegister, pending, error, onClearError }: LoginPageProps) {
  const [mode, setMode] = useState<Mode>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');

  const isRegister = mode === 'register';

  // 密码长度在本地先拦一道：否则用户要等一次往返才知道太短。
  // 这只是体验优化，真正的校验在服务端（见 lib/auth.ts 的说明）。
  const tooShort = isRegister && password.length > 0 && password.length < MIN_PASSWORD_LENGTH;
  const canSubmit = email.trim().length > 0 && password.length > 0 && !tooShort && !pending;

  const handleSubmit = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      if (!canSubmit) return;

      await (isRegister ? onRegister : onLogin)(email.trim(), password);
    },
    [canSubmit, email, isRegister, onLogin, onRegister, password],
  );

  const switchMode = useCallback(() => {
    setMode((previous) => (previous === 'login' ? 'register' : 'login'));
    // 切换模式后上一条错误（如「邮箱已注册」）不再适用，留着会误导
    onClearError();
  }, [onClearError]);

  const invalid = tooShort || Boolean(error);

  return (
    <div className="flex h-screen w-screen items-center justify-center bg-background px-4 font-sans text-foreground antialiased">
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
            <Input
              id="auth-password"
              type="password"
              name="password"
              autoComplete={isRegister ? 'new-password' : 'current-password'}
              required
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              aria-invalid={invalid || undefined}
            />
            {isRegister ? (
              <p className={tooShort ? 'text-xs text-destructive' : 'text-xs text-muted-foreground'}>
                至少 {MIN_PASSWORD_LENGTH} 个字符
              </p>
            ) : null}
          </div>

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
