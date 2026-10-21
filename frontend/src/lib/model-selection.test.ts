import { describe, expect, it } from 'vitest';

import { findModelByPrefix, readModelSelection } from './model-selection';
import type { ModelItem } from '../types';

const model = (id: string, overrides: Partial<ModelItem> = {}): ModelItem => ({
  id,
  name: id,
  provider: '默认网关',
  reasoning: false,
  supported_reasoning_levels: [],
  is_default: false,
  ...overrides,
});

describe('readModelSelection', () => {
  const models = [model('auto/a', { name: 'A', is_default: true }), model('auto/b', { name: 'B' })];

  it('当前模型在列表里时返回对应条目', () => {
    const state = readModelSelection(models, 'auto/b');

    expect(state.selected?.id).toBe('auto/b');
    expect(state.selectedMissing).toBe(false);
    expect(state.defaultModel?.id).toBe('auto/a');
  });

  it('列表非空但当前 id 不在里面 = 当前模型已不可用', () => {
    const state = readModelSelection(models, 'auto/removed');

    expect(state.selected).toBeNull();
    expect(state.selectedMissing).toBe(true);
    // 默认模型仍然可用：组件用它提供「一键切到默认」的出路
    expect(state.defaultModel?.id).toBe('auto/a');
  });

  it('列表为空不做「不可用」判断（还没加载或加载失败，不能冤枉模型）', () => {
    expect(readModelSelection([], 'auto/a').selectedMissing).toBe(false);
  });

  it('没有当前模型时 selected 为 null，但默认模型照常返回', () => {
    const state = readModelSelection(models, null);

    expect(state.selected).toBeNull();
    expect(state.selectedMissing).toBe(false);
    expect(state.defaultModel?.id).toBe('auto/a');
  });

  it('没有任何条目带 is_default 时不猜第一条', () => {
    expect(readModelSelection([model('auto/a'), model('auto/b')], 'auto/a').defaultModel).toBeNull();
  });
});

describe('findModelByPrefix', () => {
  const list = [
    model('auto/a', { name: 'DeepSeek V4.1 Flash' }),
    model('auto/b', { name: 'Gemini 3.8 Flash' }),
    model('auto/c', { name: 'Kimi K3' }),
    model('auto/d', { name: 'DeepSeek R2', provider: '备用网关' }),
  ];

  it('从当前位置之后找，找不到再从列表头绕回', () => {
    expect(findModelByPrefix(list, 'g', 0)).toBe(1);
    expect(findModelByPrefix(list, 'd', 1)).toBe(3);
    // 当前已在最后一条：从头找
    expect(findModelByPrefix(list, 'd', 3)).toBe(0);
  });

  it('大小写不敏感，也能按提供商与上游 model_id 匹配', () => {
    expect(findModelByPrefix(list, 'K', 0)).toBe(2);
    expect(findModelByPrefix([model('x', { name: 'X', provider: '备用网关' })], '备', 0)).toBe(0);
  });

  it('多字符、空白与没有命中都返回 -1，让调用方保持不动', () => {
    expect(findModelByPrefix(list, 'gz', 0)).toBe(-1);
    expect(findModelByPrefix(list, ' ', 0)).toBe(-1);
    expect(findModelByPrefix(list, 'z', 0)).toBe(-1);
    expect(findModelByPrefix([], 'd', 0)).toBe(-1);
  });
});
