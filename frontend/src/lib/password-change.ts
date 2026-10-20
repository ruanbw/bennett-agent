/**
 * 改密表单的纯校验。
 *
 * 放在 lib 而不是组件里：规则顺序会直接影响用户体验（先报「密码太短」还是先报
 * 「两次不一致」），而 vitest 的 node 环境跑不了组件。
 *
 * 校验只是**即时反馈**：服务端有同一套长度规则，且只有服务端算数。
 */

import { MIN_PASSWORD_LENGTH } from './constants';

export interface PasswordChangeDraft {
  current: string;
  next: string;
  confirm: string;
}

export type PasswordChangeField = 'current' | 'next' | 'confirm';

export interface PasswordChangeIssue {
  field: PasswordChangeField;
  message: string;
}

/**
 * 返回第一条不满足的规则；全部通过返回 null。
 *
 * 顺序：当前密码 → 长度 → 新旧相同 → 两次一致。先报「太短」再报「不一致」——
 * 长度是用户自己能立刻改的，而两次不一致往往是因为第一遍就输错了。
 */
export function validatePasswordChange(draft: PasswordChangeDraft): PasswordChangeIssue | null {
  if (!draft.current) {
    return { field: 'current', message: '请输入当前密码' };
  }
  if (draft.next.length < MIN_PASSWORD_LENGTH) {
    return { field: 'next', message: `新密码至少需要 ${MIN_PASSWORD_LENGTH} 个字符` };
  }
  if (draft.next === draft.current) {
    return { field: 'next', message: '新密码不能与当前密码相同' };
  }
  if (draft.confirm !== draft.next) {
    return { field: 'confirm', message: '两次输入的新密码不一致' };
  }
  return null;
}
