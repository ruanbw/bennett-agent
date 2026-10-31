/**
 * zod schema 边界：表单校验模型是前后端对齐的唯一事实来源。
 *
 * admin.test.ts 已经从「提交链路」的角度测过 validateProviderDraft / validateModelDraft；
 * 这个文件从 **schema 本身** 的角度补边界：合法输入全字段通过、字段级约束（长度上限、
 * 正整数、协议前缀）各自独立成立、`validate` 包装返回第一个问题且文案可读。
 */

import { describe, expect, it } from 'vitest';

import {
  MAX_MODEL_DISPLAY_NAME_CHARS,
  modelDraftSchema,
  providerDraftSchema,
  validate,
  validateModelDraft,
  validateProviderDraft,
} from './schemas';

/** provider 草稿：默认合法（自定义预设 + http 地址），用例只覆盖要破坏的字段。 */
function providerDraft(overrides: Record<string, unknown> = {}) {
  return {
    name: 'DeepSeek',
    baseUrl: 'https://api.deepseek.com',
    presetKey: 'custom',
    apiKey: '',
    enabled: true,
    clearApiKey: false,
    ...overrides,
  };
}

/** model 草稿：默认合法（名称 + 正整数窗口，输出上限留空）。 */
function modelDraft(overrides: Record<string, unknown> = {}) {
  return {
    displayName: 'DeepSeek V4',
    contextWindow: '128000',
    maxOutputTokens: '',
    reasoning: false,
    ...overrides,
  };
}

describe('providerDraftSchema', () => {
  it('合法草稿通过 parse（superRefine 不误伤完整数据）', () => {
    expect(providerDraftSchema.safeParse(providerDraft()).success).toBe(true);
  });

  it('空名 / 空白名被拒，文案指向必填', () => {
    expect(providerDraftSchema.safeParse(providerDraft({ name: '' })).success).toBe(false);
    const issue = providerDraftSchema.safeParse(providerDraft({ name: '   ' })).error!.issues[0];
    expect(issue.path).toEqual(['name']);
    expect(issue.message).toBe('请填写名称');
  });

  it('地址空 + 自定义预设被拒，提示二选一', () => {
    const issue = providerDraftSchema.safeParse(
      providerDraft({ baseUrl: '', presetKey: 'custom' }),
    ).error!.issues[0];

    expect(issue.path).toEqual(['baseUrl']);
    expect(issue.message).toBe('请填写接口地址，或选择一个厂商预设');
  });

  it('地址协议大小写敏感：HTTPS:// 是本地放行、服务端必拒的值', () => {
    const issue = providerDraftSchema.safeParse(
      providerDraft({ baseUrl: 'HTTPS://api.example.com' }),
    ).error!.issues[0];

    expect(issue.path).toEqual(['baseUrl']);
    expect(issue.message).toContain('http://');
  });

  it('协议判断作用于 trim 后的值（首尾空白不算合法前缀的烟雾弹）', () => {
    expect(providerDraftSchema.safeParse(providerDraft({ baseUrl: '  https://x.dev  ' })).success).toBe(
      true,
    );
    expect(providerDraftSchema.safeParse(providerDraft({ baseUrl: '  ftp://x.dev  ' })).success).toBe(
      false,
    );
  });

  it('其他类型字段缺省不通过：schema 锁住整个草稿形状，不只是名字和地址', () => {
    const { success } = providerDraftSchema.safeParse({ name: 'DeepSeek', baseUrl: '' });
    expect(success).toBe(false);
  });
});

describe('modelDraftSchema', () => {
  it('合法草稿通过 parse：输出上限留空 = 未知', () => {
    expect(modelDraftSchema.safeParse(modelDraft()).success).toBe(true);
  });

  it(`显示名称按码点计长，超过 ${MAX_MODEL_DISPLAY_NAME_CHARS} 字被拒`, () => {
    expect(
      modelDraftSchema.safeParse(modelDraft({ displayName: '模'.repeat(MAX_MODEL_DISPLAY_NAME_CHARS) }))
        .success,
    ).toBe(true);
    const issue = modelDraftSchema.safeParse(
      modelDraft({ displayName: '模'.repeat(MAX_MODEL_DISPLAY_NAME_CHARS + 1) }),
    ).error!.issues[0];
    expect(issue.path).toEqual(['displayName']);
    expect(issue.message).toContain(String(MAX_MODEL_DISPLAY_NAME_CHARS));
  });

  it('上下文窗口：非数字、负数、零、小数都拒绝', () => {
    for (const bad of ['12a', '-1', '0', '1.5', '1e3']) {
      const issue = modelDraftSchema.safeParse(modelDraft({ contextWindow: bad })).error!.issues[0];
      expect(issue.path, `${bad} 应指向 contextWindow`).toEqual(['contextWindow']);
      expect(issue.message).toBe('上下文窗口必须是正整数，或留空');
    }
  });

  it('上下文窗口留空 / 纯空白视为未知（合法）', () => {
    expect(modelDraftSchema.safeParse(modelDraft({ contextWindow: '' })).success).toBe(true);
    expect(modelDraftSchema.safeParse(modelDraft({ contextWindow: '  ' })).success).toBe(true);
  });

  it('超大数字超出安全整数范围时拒绝（提交前拦下，不让后端收到失真值）', () => {
    expect(modelDraftSchema.safeParse(modelDraft({ contextWindow: '9'.repeat(30) })).success).toBe(false);
  });

  it('两个上限字段各自独立校验，错误只报在坏的那个上', () => {
    const issue = modelDraftSchema.safeParse(
      modelDraft({ contextWindow: '128000', maxOutputTokens: 'x' }),
    ).error!.issues[0];
    expect(issue.path).toEqual(['maxOutputTokens']);
  });
});

describe('validate 包装', () => {
  it('通过时返回 null，失败时返回第一个问题的字段与文案', () => {
    expect(validate(providerDraftSchema, providerDraft())).toBeNull();
    expect(validateProviderDraft(providerDraft({ name: ' ' }))).toEqual({
      field: 'name',
      message: '请填写名称',
    });
  });

  it('schema 直接解析非对象输入也不抛异常，而是返回可读问题', () => {
    const result = validateModelDraft(null);
    expect(result).not.toBeNull();
    expect(typeof result!.message).toBe('string');
    expect(result!.message.length).toBeGreaterThan(0);
  });
});
