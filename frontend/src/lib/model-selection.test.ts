import { describe, expect, it } from 'vitest';

import { readModelSelection } from './model-selection';
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
