import { describe, expect, it } from 'vitest';

import { modelFullLabel, modelSubtitle } from './model-identity';

describe('modelSubtitle', () => {
  it('把提供商与上游模型 ID 拼成第二行', () => {
    expect(modelSubtitle({ provider: '默认网关', model_id: 'auto/deepseek-v4.1-flash' })).toBe(
      '默认网关 · auto/deepseek-v4.1-flash'
    );
  });

  it('同一提供商下三条同名模型仍然可区分', () => {
    const subtitles = ['auto/x', 'workbuddy/x', 'workbuddy-ai/x'].map((model_id) =>
      modelSubtitle({ provider: '默认网关', model_id })
    );

    expect(new Set(subtitles).size).toBe(3);
  });

  it('旧版网关路径没有 model_id 时只显示提供商（不凭空加分隔符）', () => {
    expect(modelSubtitle({ provider: 'openai', model_id: null })).toBe('openai');
    expect(modelSubtitle({ provider: 'openai' })).toBe('openai');
  });

  it('空白与缺少提供商时不留悬挂的分隔符', () => {
    expect(modelSubtitle({ provider: '  ', model_id: 'auto/x' })).toBe('auto/x');
    expect(modelSubtitle({ provider: '默认网关', model_id: '   ' })).toBe('默认网关');
    expect(modelSubtitle({ provider: null, model_id: null })).toBe('');
  });
});

describe('modelFullLabel', () => {
  it('名称在前、身份在后，供悬停提示使用', () => {
    expect(
      modelFullLabel('Deepseek-V4.1-Flash', { provider: '默认网关', model_id: 'auto/deepseek-v4.1-flash' })
    ).toBe('Deepseek-V4.1-Flash · 默认网关 · auto/deepseek-v4.1-flash');
  });

  it('没有名称时不出前导分隔符', () => {
    expect(modelFullLabel('  ', { provider: '默认网关', model_id: 'auto/x' })).toBe('默认网关 · auto/x');
  });
});
