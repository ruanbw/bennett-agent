/**
 * 改密表单的静态结构锁。
 *
 * 交互（显隐、校验聚焦、提交）由 Playwright 覆盖；这里锁那些「改样式时最容易顺手
 * 改坏」的语义：三个字段默认密文、显隐按钮各自有可读名字与受控关系、长度规则和
 * 「所有设备下线」的后果必须提前写出来。
 */

import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

// 表单只 import 了 changePassword；静态渲染不需要真实的 db/api 链路
vi.mock('../lib/auth', () => ({ changePassword: vi.fn() }));

import { MIN_PASSWORD_LENGTH } from '../lib/constants';
import { PasswordChangeForm } from './PasswordChangeForm';

const render = () => renderToStaticMarkup(createElement(PasswordChangeForm, {}));

describe('PasswordChangeForm', () => {
  it('三个密码字段默认密文，且各自有可读的显隐按钮', () => {
    const html = render();

    expect(html.match(/type="password"/g)).toHaveLength(3);
    expect(html).toContain('aria-label="显示当前密码"');
    expect(html).toContain('aria-label="显示新密码"');
    expect(html).toContain('aria-label="显示确认新密码"');
    // 三个按钮的受控关系必须指向各自的输入框，不能都默认叫「显示密码」
    expect(html).toContain('aria-controls="password-current"');
    expect(html).toContain('aria-controls="password-new"');
    expect(html).toContain('aria-controls="password-confirm"');
    expect(html.match(/aria-pressed="false"/g)).toHaveLength(3);
  });

  it('提前说明长度规则与「所有设备重新登录」的后果', () => {
    const html = render();

    expect(html).toContain(`新密码至少需要 ${MIN_PASSWORD_LENGTH} 个字符`);
    expect(html).toContain('所有设备都需要用新密码重新登录');
  });

  it('自动填充语义区分当前密码与新密码', () => {
    const html = render();

    expect(html.match(/autocomplete="current-password"/gi)).toHaveLength(1);
    expect(html.match(/autocomplete="new-password"/gi)).toHaveLength(2);
  });
});
