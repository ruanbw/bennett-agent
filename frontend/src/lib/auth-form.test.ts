import { describe, expect, it } from 'vitest';

import { readAuthFieldState } from './auth-form';

const base = { mode: 'login' as const, password: 'long-enough', error: null, minPasswordLength: 8 };

describe('readAuthFieldState', () => {
  it('登录模式下服务端报错不冒充字段校验结果', () => {
    const state = readAuthFieldState({ ...base, error: '邮箱或密码错误' });

    expect(state.emailInvalid).toBe(false);
    expect(state.passwordInvalid).toBe(false);
    // 但仍要通过 describedby 让读屏听到结论
    expect(state.emailDescribedBy).toBe('auth-error');
    expect(state.passwordDescribedBy).toBe('auth-error');
  });

  it('注册模式下密码太短只标密码框', () => {
    const state = readAuthFieldState({ ...base, mode: 'register', password: 'abc' });

    expect(state.passwordInvalid).toBe(true);
    expect(state.emailInvalid).toBe(false);
    // 长度提示只挂密码框，不能挂到邮箱上
    expect(state.emailDescribedBy).toBe('');
    expect(state.passwordDescribedBy).toBe('auth-password-hint');
  });

  it('密码太短 + 服务端错误时两个 id 都挂在密码框', () => {
    const state = readAuthFieldState({
      ...base,
      mode: 'register',
      password: 'abc',
      error: '该邮箱已注册',
    });

    expect(state.passwordDescribedBy).toBe('auth-error auth-password-hint');
    expect(state.emailDescribedBy).toBe('auth-error');
  });

  it('登录模式下短密码不算错（判断交给服务端，避免旧账号被本地拦住）', () => {
    const state = readAuthFieldState({ ...base, mode: 'login', password: 'abc' });
    expect(state.passwordInvalid).toBe(false);
    expect(state.emailDescribedBy).toBe('');
    expect(state.passwordDescribedBy).toBe('');
  });

  it('没有错误时注册模式只给密码框挂长度提示', () => {
    const state = readAuthFieldState({ ...base, mode: 'register' });

    expect(state.emailDescribedBy).toBe('');
    expect(state.passwordDescribedBy).toBe('auth-password-hint');
  });
});
