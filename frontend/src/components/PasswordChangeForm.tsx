/**
 * 修改密码表单。
 *
 * 成功后服务端会让该账号**所有会话**失效（包括当前这个），所以表单不显示「已保存」
 * 就算完事——它调用 `onPasswordChanged`，由 App 把登录态切回未登录并说明原因。
 *
 * 失败分两类：
 * - 表单问题（太短/不一致/新旧相同）：本地校验，不发请求；
 * - 服务端问题（当前密码不正确、策略拒绝、网络）：就地显示服务端文案，用户留在原地
 *   重试。400 不会被全局 401 处理当成会话过期，这一点由后端的错误码选择保证。
 */

import { useCallback, useState, type FormEvent } from 'react';
import { KeyRound, Loader2 } from 'lucide-react';

import { Input } from './ui/input';
import { Button } from './ui/button';
import { changePassword } from '../lib/auth';
import { validatePasswordChange } from '../lib/password-change';

export interface PasswordChangeFormProps {
  /** 密码已修改（服务端已失效所有会话）；App 用它退回登录页。 */
  onPasswordChanged?: () => void;
}

export function PasswordChangeForm({ onPasswordChanged }: PasswordChangeFormProps) {
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      if (submitting) return;

      const issue = validatePasswordChange({ current, next, confirm });
      if (issue) {
        setError(issue.message);
        return;
      }

      setSubmitting(true);
      setError(null);
      try {
        const result = await changePassword({ currentPassword: current, newPassword: next });
        if (!result.ok) {
          setError(result.message);
          return;
        }
        // 清空表单：即使回调缺失（例如测试里单独渲染），也不把密码留在内存/输入框里
        setCurrent('');
        setNext('');
        setConfirm('');
        onPasswordChanged?.();
      } finally {
        setSubmitting(false);
      }
    },
    [confirm, current, next, onPasswordChanged, submitting],
  );

  return (
    <form onSubmit={handleSubmit} className="space-y-2">
      <div className="rounded-lg border border-border/70 bg-muted/40 p-3 space-y-2">
        <label className="block">
          <span className="mb-1 block text-[11px] text-muted-foreground">当前密码</span>
          <Input
            type="password"
            value={current}
            onChange={(event) => setCurrent(event.target.value)}
            autoComplete="current-password"
            aria-invalid={error !== null && !current ? true : undefined}
            className="h-8"
          />
        </label>

        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          <label className="block">
            <span className="mb-1 block text-[11px] text-muted-foreground">新密码</span>
            <Input
              type="password"
              value={next}
              onChange={(event) => setNext(event.target.value)}
              autoComplete="new-password"
              className="h-8"
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-[11px] text-muted-foreground">确认新密码</span>
            <Input
              type="password"
              value={confirm}
              onChange={(event) => setConfirm(event.target.value)}
              autoComplete="new-password"
              className="h-8"
            />
          </label>
        </div>

        {error && (
          <p role="alert" className="text-[11px] text-destructive">
            {error}
          </p>
        )}

        <div className="flex items-center justify-between gap-2">
          <p className="text-[10.5px] leading-relaxed text-muted-foreground/80">
            修改成功后所有设备都需要用新密码重新登录。
          </p>
          <Button
            type="submit"
            size="sm"
            variant="outline"
            disabled={submitting}
            className="shrink-0 text-[11px]"
          >
            {submitting ? (
              <Loader2 className="size-3 animate-spin" aria-hidden="true" />
            ) : (
              <KeyRound className="size-3" aria-hidden="true" />
            )}
            修改密码
          </Button>
        </div>
      </div>
    </form>
  );
}
