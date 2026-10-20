/**
 * 修改密码表单。
 *
 * 成功后服务端会让该账号**所有会话**失效（包括当前这个），所以表单不显示「已保存」
 * 就算完事——它调用 `onPasswordChanged`，由 App 把登录态切回未登录并说明原因。
 *
 * 失败分两类：
 * - 表单问题（太短/不一致/新旧相同）：本地校验，不发请求；错误会**聚焦到对应字段**，
 *   并用 `aria-invalid` / `aria-describedby` 把原因挂到那个输入框上；
 * - 服务端问题（当前密码不正确、策略拒绝、网络）：就地显示服务端文案，用户留在原地
 *   重试。400 不会被全局 401 处理当成会话过期，这一点由后端的错误码选择保证。
 *
 * 三个字段各自有显隐按钮（默认密文）。改密时用户往往要连输三遍，尤其是手机上没有
 * 键盘反馈；能核对一遍再提交比事后发现两次不一致更省事。
 */

import { useCallback, useRef, useState, type FormEvent, type RefObject } from 'react';
import { Eye, EyeOff, KeyRound, Loader2 } from 'lucide-react';

import { Input } from './ui/input';
import { Button } from './ui/button';
import { changePassword } from '../lib/auth';
import { MIN_PASSWORD_LENGTH } from '../lib/constants';
import { validatePasswordChange, type PasswordChangeField } from '../lib/password-change';

export interface PasswordChangeFormProps {
  /** 密码已修改（服务端已失效所有会话）；App 用它退回登录页。 */
  onPasswordChanged?: () => void;
}

/** 错误文案的 id：只有出错的那个字段用 aria-describedby 指向它。 */
const ERROR_ID = 'password-change-error';

type VisibilityState = Record<PasswordChangeField, boolean>;

const HIDDEN_PASSWORDS: VisibilityState = { current: false, next: false, confirm: false };

interface PasswordFieldProps {
  id: string;
  name: string;
  label: string;
  value: string;
  autoComplete: string;
  disabled: boolean;
  visible: boolean;
  invalid: boolean;
  describedBy?: string;
  inputRef: RefObject<HTMLInputElement | null>;
  onChange: (value: string) => void;
  onToggleVisibility: () => void;
}

function PasswordField({
  id,
  name,
  label,
  value,
  autoComplete,
  disabled,
  visible,
  invalid,
  describedBy,
  inputRef,
  onChange,
  onToggleVisibility,
}: PasswordFieldProps) {
  return (
    <div className="min-w-0">
      {/* label 与按钮是兄弟节点，不把交互元素嵌进 label：点眼睛只该切显隐，
          不应触发 label 对输入框的激活。 */}
      <label htmlFor={id} className="mb-1 block text-[11px] text-muted-foreground">
        {label}
      </label>
      <div className="relative">
        <Input
          ref={inputRef}
          id={id}
          name={name}
          type={visible ? 'text' : 'password'}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          autoComplete={autoComplete}
          disabled={disabled}
          aria-invalid={invalid || undefined}
          aria-describedby={describedBy}
          className="h-9 pr-9"
        />
        <button
          type="button"
          onClick={onToggleVisibility}
          disabled={disabled}
          // 三个按钮的默认名字不能都叫「显示密码」：读屏与浏览器自动化都区分不出是哪一项
          aria-label={`${visible ? '隐藏' : '显示'}${label}`}
          aria-pressed={visible}
          aria-controls={id}
          title={`${visible ? '隐藏' : '显示'}${label}`}
          className="absolute right-1 top-1/2 flex size-7 -translate-y-1/2 cursor-pointer items-center justify-center rounded-md text-muted-foreground transition hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {visible ? (
            <EyeOff size={14} aria-hidden="true" />
          ) : (
            <Eye size={14} aria-hidden="true" />
          )}
        </button>
      </div>
    </div>
  );
}

export function PasswordChangeForm({ onPasswordChanged }: PasswordChangeFormProps) {
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [visible, setVisible] = useState<VisibilityState>(HIDDEN_PASSWORDS);
  const [submitting, setSubmitting] = useState(false);
  // 带上出错字段：本地校验知道该聚焦谁，服务端错误只知道文案。
  const [error, setError] = useState<{ message: string; field?: PasswordChangeField } | null>(null);

  const currentRef = useRef<HTMLInputElement>(null);
  const nextRef = useRef<HTMLInputElement>(null);
  const confirmRef = useRef<HTMLInputElement>(null);

  const updateField = useCallback((field: PasswordChangeField, value: string) => {
    if (field === 'current') setCurrent(value);
    else if (field === 'next') setNext(value);
    else setConfirm(value);
    // 用户已经在改这个字段（或服务端错误只会给个笼统文案），旧提示不再成立
    setError((prev) => (prev && (prev.field === undefined || prev.field === field) ? null : prev));
  }, []);

  const toggleVisibility = useCallback((field: PasswordChangeField) => {
    setVisible((prev) => ({ ...prev, [field]: !prev[field] }));
  }, []);

  const handleSubmit = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      if (submitting) return;

      const issue = validatePasswordChange({ current, next, confirm });
      if (issue) {
        setError(issue);
        // 仅显示文案不够：键盘/读屏用户还要自己找是哪一栏。聚焦 + aria-invalid 一起做。
        const refs = { current: currentRef, next: nextRef, confirm: confirmRef };
        refs[issue.field].current?.focus();
        return;
      }

      setSubmitting(true);
      setError(null);
      try {
        const result = await changePassword({ currentPassword: current, newPassword: next });
        if (!result.ok) {
          setError({ message: result.message });
          return;
        }
        // 清空表单：即使回调缺失（例如测试里单独渲染），也不把密码留在内存/输入框里
        setCurrent('');
        setNext('');
        setConfirm('');
        setVisible(HIDDEN_PASSWORDS);
        onPasswordChanged?.();
      } finally {
        setSubmitting(false);
      }
    },
    [confirm, current, next, onPasswordChanged, submitting],
  );

  return (
    <form onSubmit={handleSubmit} aria-busy={submitting} className="space-y-2">
      <div className="space-y-2 rounded-lg border border-border/70 bg-muted/40 p-3">
        <PasswordField
          id="password-current"
          name="current-password"
          label="当前密码"
          value={current}
          autoComplete="current-password"
          disabled={submitting}
          visible={visible.current}
          invalid={error?.field === 'current'}
          describedBy={error?.field === 'current' ? ERROR_ID : undefined}
          inputRef={currentRef}
          onChange={(value) => updateField('current', value)}
          onToggleVisibility={() => toggleVisibility('current')}
        />

        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          <PasswordField
            id="password-new"
            name="new-password"
            label="新密码"
            value={next}
            autoComplete="new-password"
            disabled={submitting}
            visible={visible.next}
            invalid={error?.field === 'next'}
            describedBy={error?.field === 'next' ? ERROR_ID : undefined}
            inputRef={nextRef}
            onChange={(value) => updateField('next', value)}
            onToggleVisibility={() => toggleVisibility('next')}
          />
          <PasswordField
            id="password-confirm"
            name="confirm-password"
            label="确认新密码"
            value={confirm}
            autoComplete="new-password"
            disabled={submitting}
            visible={visible.confirm}
            invalid={error?.field === 'confirm'}
            describedBy={error?.field === 'confirm' ? ERROR_ID : undefined}
            inputRef={confirmRef}
            onChange={(value) => updateField('confirm', value)}
            onToggleVisibility={() => toggleVisibility('confirm')}
          />
        </div>

        {error && (
          <p id={ERROR_ID} role="alert" className="text-[11px] text-destructive">
            {error.message}
          </p>
        )}

        <div className="flex items-center justify-between gap-2">
          <p className="text-[10.5px] leading-relaxed text-muted-foreground/80">
            新密码至少需要 {MIN_PASSWORD_LENGTH} 个字符；修改成功后所有设备都需要用新密码重新登录。
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
