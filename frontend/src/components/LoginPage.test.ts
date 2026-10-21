/**
 * 登录页的静态结构锁。
 *
 * 交互（显示/隐藏密码）由 Playwright 覆盖；这里只锁那些「改样式时最容易顺手改坏」的
 * 语义：密码字段默认必须密文、显隐按钮要有可读名字与受控关系、错误要有 role=alert、
 * 邮箱字段要告诉移动端键盘不要自动大写/纠错。
 */

import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { LoginPage } from './LoginPage';

const render = (overrides: Partial<Parameters<typeof LoginPage>[0]> = {}) =>
  renderToStaticMarkup(
    createElement(LoginPage, {
      onLogin: vi.fn(async () => true),
      onRegister: vi.fn(async () => 'ok' as const),
      pending: false,
      error: null,
      onClearError: vi.fn(),
      ...overrides,
    }),
  );

describe('LoginPage', () => {
  it('密码默认是密文，并带一个可读的「显示密码」按钮', () => {
    const html = render();

    expect(html).toContain('id="auth-password"');
    // SSR 输出里属性保持 React 的写法与顺序：`type` 在 `id` 之前
    expect(html).toMatch(/type="password"[^>]*id="auth-password"/);
    expect(html).toContain('aria-label="显示密码"');
    expect(html).toContain('aria-pressed="false"');
    // 显隐按钮必须声明它控制哪个输入框
    expect(html).toContain('aria-controls="auth-password"');
  });

  it('邮箱字段关闭自动大写与拼写纠正（防止 Email@… 这类输入）', () => {
    const html = render();

    expect(html).toContain('autoCapitalize="none"');
    expect(html).toContain('spellCheck="false"');
    expect(html).toContain('autoComplete="username"');
  });

  it('错误用 role=alert 呈现（异步出现的消息需要读屏主动播报）', () => {
    const html = render({ error: '邮箱或密码错误' });

    expect(html).toContain('role="alert"');
    expect(html).toContain('邮箱或密码错误');
  });

  it('服务端错误不把输入框标成 aria-invalid，只用 describedby 关联', () => {
    const html = render({ error: '该邮箱已注册' });

    // 服务端只给了一句结论，不能冒充「邮箱/密码哪个字段填错了」。
    // 注意 Input 的 class 里带 `aria-invalid:` 变体，只能断言属性本身。
    expect(html).not.toContain('aria-invalid="true"');
    expect(html).toContain('aria-describedby="auth-error"');
    expect(html).toContain('id="auth-error"');
  });
});
