import { describe, expect, it } from 'vitest';

import {
  AdminApiError,
  buildProviderPayload,
  clampUsersOffset,
  describeAdminError,
  describeFailure,
  formatContextWindow,
  validateProviderDraft,
  type ProviderDraft,
} from './admin';

/** 表单草稿默认带前后空白：trim 是这些用例的主要断言对象之一。 */
function draft(overrides: Partial<ProviderDraft> = {}): ProviderDraft {
  return {
    name: '  DeepSeek  ',
    baseUrl: '  https://api.deepseek.com  ',
    apiKey: '',
    enabled: true,
    clearApiKey: false,
    ...overrides,
  };
}

describe('validateProviderDraft', () => {
  it('合法草稿（含首尾空白）通过', () => {
    expect(validateProviderDraft(draft())).toBeNull();
  });

  it('名称不能为空', () => {
    expect(validateProviderDraft(draft({ name: '   ' }))).toEqual({
      field: 'name',
      message: '请填写名称',
    });
  });

  it('接口地址不能为空', () => {
    expect(validateProviderDraft(draft({ baseUrl: '  ' }))).toEqual({
      field: 'baseUrl',
      message: '请填写接口地址',
    });
  });

  it('接口地址只接受 http(s)，与后端规则保持一致', () => {
    expect(validateProviderDraft(draft({ baseUrl: 'ftp://example.com' }))).toEqual({
      field: 'baseUrl',
      message: '接口地址必须以 http:// 或 https:// 开头',
    });
    // 后端是大小写敏感的 startswith：本地不能放行一个服务端必拒的写法
    expect(validateProviderDraft(draft({ baseUrl: 'HTTPS://example.com' }))?.field).toBe('baseUrl');
  });

  it('校验顺序：先名称、再地址（一次只提示第一条）', () => {
    expect(validateProviderDraft(draft({ name: '', baseUrl: '' }))?.field).toBe('name');
  });
});

describe('clampUsersOffset', () => {
  it('总数不变时不动页码', () => {
    expect(clampUsersOffset(0, 20, 45)).toBe(0);
    expect(clampUsersOffset(20, 20, 45)).toBe(20);
    expect(clampUsersOffset(40, 20, 45)).toBe(40);
  });

  it('封禁最后一页的唯一用户后夹回最后一页', () => {
    // 21 个用户时第 2 页只有 1 个；封掉后总数 20，offset=20 越界
    expect(clampUsersOffset(20, 20, 20)).toBe(0);
  });

  it('总数变少但当前页仍存在时保留原页码', () => {
    expect(clampUsersOffset(20, 20, 39)).toBe(20);
  });

  it('空列表 / 非法分页大小回到第一页', () => {
    expect(clampUsersOffset(40, 20, 0)).toBe(0);
    expect(clampUsersOffset(40, 0, 100)).toBe(0);
    expect(clampUsersOffset(-20, 20, 100)).toBe(0);
  });
});

describe('buildProviderPayload', () => {
  it('创建时 apiKey 为空就不发 api_key 字段', () => {
    const payload = buildProviderPayload(draft(), 'create');

    expect(payload).not.toHaveProperty('api_key');
    expect(payload).toEqual({
      name: 'DeepSeek',
      base_url: 'https://api.deepseek.com',
      enabled: true,
    });
  });

  it('创建时 apiKey 非空则带上，并去掉首尾空白', () => {
    const payload = buildProviderPayload(draft({ apiKey: '  sk-abc123  ' }), 'create');

    expect(payload.api_key).toBe('sk-abc123');
    expect(payload.name).toBe('DeepSeek');
    expect(payload.base_url).toBe('https://api.deepseek.com');
  });

  it('编辑时 apiKey 为空表示「不修改」：请求体里没有 api_key', () => {
    const payload = buildProviderPayload(draft({ apiKey: '   ' }), 'edit');

    expect(payload).not.toHaveProperty('api_key');
    expect(payload.enabled).toBe(true);
  });

  it('编辑时勾选清除密钥就发空串（后端据此清空已保存的值）', () => {
    const payload = buildProviderPayload(draft({ apiKey: 'sk-ignored', clearApiKey: true }), 'edit');

    expect(payload.api_key).toBe('');
  });

  it('编辑时输入了新密钥则发新值', () => {
    const payload = buildProviderPayload(draft({ apiKey: ' sk-new ' }), 'edit');

    expect(payload.api_key).toBe('sk-new');
  });

  it('enabled 原样透传（false 不能被当成「未设置」丢掉）', () => {
    const payload = buildProviderPayload(draft({ enabled: false }), 'edit');

    expect(payload.enabled).toBe(false);
  });
});

describe('describeAdminError', () => {
  it('403 翻译成权限提示（不再只提「超级管理员」：admin 也能进后台，但部分动作不行）', () => {
    expect(describeAdminError({ status: 403 })).toBe('权限不足（需要管理员或超级管理员）');
  });

  it('框架自带的 "Forbidden" 不算业务说明，同样翻译成权限提示', () => {
    expect(describeAdminError({ status: 403, detail: 'Forbidden' })).toBe(
      '权限不足（需要管理员或超级管理员）'
    );
  });

  it('后端中文 detail 原样返回', () => {
    const detail = '停用的模型不能设为默认模型';
    expect(describeAdminError({ status: 400, detail })).toBe(detail);
  });

  it('404 有专门文案', () => {
    expect(describeAdminError({ status: 404 })).toBe('目标不存在，可能已被其他管理员删除');
  });

  it('未知错误走兜底文案', () => {
    expect(describeAdminError({})).toBe('操作失败，请稍后重试');
    expect(describeAdminError(new Error('boom'))).toBe('操作失败，请稍后重试');
  });
});

describe('describeFailure', () => {
  it('AdminApiError 直接使用已翻译的文案', () => {
    expect(describeFailure(new AdminApiError('停用的模型不能设为默认模型', 400))).toBe(
      '停用的模型不能设为默认模型',
    );
  });

  it('网络层异常没有状态码，走兜底文案而不是英文原文', () => {
    expect(describeFailure(new TypeError('Failed to fetch'))).toBe('操作失败，请稍后重试');
  });
});

describe('formatContextWindow', () => {
  it('窗口未知时给出可读占位', () => {
    expect(formatContextWindow(null)).toBe('窗口未知');
    expect(formatContextWindow(0)).toBe('窗口未知');
  });

  it('千级窗口按 K 展示', () => {
    expect(formatContextWindow(128_000)).toBe('128K 上下文');
    expect(formatContextWindow(8_192)).toBe('8K 上下文');
  });

  it('百万级窗口按 M 展示', () => {
    expect(formatContextWindow(1_000_000)).toBe('1M 上下文');
    expect(formatContextWindow(2_000_000)).toBe('2M 上下文');
  });
});
