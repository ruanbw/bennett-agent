import { describe, expect, it } from 'vitest';

import {
  DEFAULT_DETAIL_LEVEL,
  DETAIL_LEVEL_STORAGE_KEY,
  detailLevelFromStorageEvent,
  normalizeDetailLevel,
  readDetailLevel,
  writeDetailLevel,
  type DetailLevelStorage,
} from './detail-level';

/** 内存版 localStorage；可注入抛错。 */
function memoryStorage(initial: Record<string, string> = {}): DetailLevelStorage & {
  data: Record<string, string>;
} {
  const data = { ...initial };
  return {
    data,
    getItem: (key) => (key in data ? data[key] : null),
    setItem: (key, value) => {
      data[key] = value;
    },
  };
}

describe('对话显示偏好的归一化', () => {
  it('默认是简洁：过程收成一行，最终回答才是主角', () => {
    expect(DEFAULT_DETAIL_LEVEL).toBe('concise');
    expect(normalizeDetailLevel(null)).toBe('concise');
    expect(normalizeDetailLevel(undefined)).toBe('concise');
    expect(normalizeDetailLevel('')).toBe('concise');
  });

  it('只接受两个白名单取值，脏值一律回落默认', () => {
    expect(normalizeDetailLevel('detailed')).toBe('detailed');
    expect(normalizeDetailLevel('concise')).toBe('concise');
    expect(normalizeDetailLevel('Detailed')).toBe('concise');
    expect(normalizeDetailLevel(1)).toBe('concise');
    expect(normalizeDetailLevel({})).toBe('concise');
  });
});

describe('读写偏好', () => {
  it('写进去再读出来是同一个值', () => {
    const storage = memoryStorage();
    writeDetailLevel('detailed', storage);
    expect(storage.data[DETAIL_LEVEL_STORAGE_KEY]).toBe('detailed');
    expect(readDetailLevel(storage)).toBe('detailed');
  });

  it('没有存储时读默认值、写是空操作，不抛', () => {
    expect(readDetailLevel(null)).toBe('concise');
    expect(() => writeDetailLevel('detailed', null)).not.toThrow();
  });

  it('存储抛异常时读回落默认、写不向上抛——偏好是增强，不能因此打挂界面', () => {
    const throwing: DetailLevelStorage = {
      getItem: () => {
        throw new Error('SecurityError');
      },
      setItem: () => {
        throw new Error('QuotaExceededError');
      },
    };
    expect(readDetailLevel(throwing)).toBe('concise');
    expect(() => writeDetailLevel('detailed', throwing)).not.toThrow();
  });
});

describe('storage 事件（多标签页同步）', () => {
  it('本键的变更被识别', () => {
    expect(detailLevelFromStorageEvent({ key: DETAIL_LEVEL_STORAGE_KEY, newValue: 'detailed' })).toBe(
      'detailed'
    );
  });

  it('别的键返回 null，调用方据此忽略', () => {
    expect(detailLevelFromStorageEvent({ key: 'theme_preference', newValue: 'dark' })).toBeNull();
  });

  it('storage.clear() 时回落默认，而不是停在旧值', () => {
    expect(detailLevelFromStorageEvent({ key: null, newValue: null })).toBe('concise');
  });
});
