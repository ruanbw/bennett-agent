import { describe, expect, it } from 'vitest';

import {
  START_SUGGESTIONS,
  availableStartSuggestions,
  pickStartSuggestions,
} from './start-suggestions';

describe('availableStartSuggestions', () => {
  it('没有联网搜索时把依赖搜索的示例整条剔除，而不是留个点不动的空壳', () => {
    const available = availableStartSuggestions(false);

    expect(available.length).toBeGreaterThan(0);
    expect(available.some((suggestion) => suggestion.requiresWebSearch)).toBe(false);
  });

  it('联网搜索可用时补回搜索示例', () => {
    const withSearch = availableStartSuggestions(true);
    const withoutSearch = availableStartSuggestions(false);

    expect(withSearch.map((suggestion) => suggestion.id)).toContain('search-industry');
    expect(withSearch.length).toBeGreaterThan(withoutSearch.length);
  });
});

describe('pickStartSuggestions', () => {
  it('默认取 4 条，且同一批内不重复', () => {
    const batch = pickStartSuggestions(false);

    expect(batch).toHaveLength(4);
    expect(new Set(batch.map((suggestion) => suggestion.id)).size).toBe(4);
  });

  it('联网不可用时，任何一批都不包含搜索示例（包括还没拿到能力清单的默认值）', () => {
    for (let offset = 0; offset < 20; offset += 1) {
      expect(pickStartSuggestions(false, offset).some((suggestion) => suggestion.requiresWebSearch)).toBe(false);
    }
  });

  it('换一批会轮转到不同的一批，连续点击能覆盖整个池子', () => {
    const first = pickStartSuggestions(true, 0);
    const second = pickStartSuggestions(true, first.length);

    expect(second.map((suggestion) => suggestion.id)).not.toEqual(first.map((suggestion) => suggestion.id));

    const all = availableStartSuggestions(true);
    const seen = new Set<string>();
    for (let offset = 0; offset < all.length; offset += 4) {
      for (const suggestion of pickStartSuggestions(true, offset)) seen.add(suggestion.id);
    }
    expect(seen.size).toBe(all.length);
  });

  it('offset 支持负数与越界（循环取）', () => {
    const all = availableStartSuggestions(false);

    expect(pickStartSuggestions(false, -1).map((suggestion) => suggestion.id)).toEqual(
      [all[all.length - 1], all[0], all[1], all[2]].map((suggestion) => suggestion.id),
    );
    expect(pickStartSuggestions(false, all.length).map((suggestion) => suggestion.id)).toEqual(
      all.slice(0, 4).map((suggestion) => suggestion.id),
    );
  });

  it('count 超过池子长度时不重复、不补空', () => {
    const all = availableStartSuggestions(false);

    expect(pickStartSuggestions(false, 0, 100)).toHaveLength(all.length);
    expect(pickStartSuggestions(false, 0, 0)).toEqual([]);
  });
});

describe('建议池数据本身', () => {
  it('文案与 id 都唯一且非空（React key 与展示都依赖它们）', () => {
    const texts = START_SUGGESTIONS.map((suggestion) => suggestion.text.trim());
    const ids = START_SUGGESTIONS.map((suggestion) => suggestion.id);

    expect(texts.every((text) => text.length > 0)).toBe(true);
    expect(new Set(texts).size).toBe(texts.length);
    expect(new Set(ids).size).toBe(ids.length);
  });
});
