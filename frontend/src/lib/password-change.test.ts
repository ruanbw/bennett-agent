/**
 * 改密校验的行为锁。
 *
 * 规则顺序是产品行为的一部分：同一份错误输入只应该提示第一条，且顺序固定
 * （否则用户会先被「两次不一致」绕一圈，其实第一遍就太短）。
 */

import { describe, expect, it } from 'vitest';

import { MIN_PASSWORD_LENGTH } from './constants';
import { validatePasswordChange, type PasswordChangeDraft } from './password-change';

const draft = (overrides: Partial<PasswordChangeDraft> = {}): PasswordChangeDraft => ({
  current: 'old-password',
  next: 'new-password',
  confirm: 'new-password',
  ...overrides,
});

describe('validatePasswordChange', () => {
  it('合法输入返回 null', () => {
    expect(validatePasswordChange(draft())).toBeNull();
  });

  it('当前密码为空时先提示它', () => {
    expect(validatePasswordChange(draft({ current: '' }))).toEqual({
      field: 'current',
      message: '请输入当前密码',
    });
  });

  it(`新密码短于 ${MIN_PASSWORD_LENGTH} 位时按长度拒绝`, () => {
    const short = 'a'.repeat(MIN_PASSWORD_LENGTH - 1);
    expect(validatePasswordChange(draft({ next: short, confirm: short }))).toEqual({
      field: 'next',
      message: `新密码至少需要 ${MIN_PASSWORD_LENGTH} 个字符`,
    });
  });

  it('新旧密码相同时拒绝（服务端同样会拦）', () => {
    expect(
      validatePasswordChange(draft({ current: 'same-password', next: 'same-password', confirm: 'same-password' })),
    ).toEqual({ field: 'next', message: '新密码不能与当前密码相同' });
  });

  it('两次输入不一致时指向确认框', () => {
    expect(validatePasswordChange(draft({ confirm: 'new-password-typo' }))).toEqual({
      field: 'confirm',
      message: '两次输入的新密码不一致',
    });
  });

  it('长度不足优先于两次不一致（先修能立刻修的那条）', () => {
    expect(validatePasswordChange(draft({ next: 'short', confirm: 'other' }))).toMatchObject({
      field: 'next',
    });
  });
});
