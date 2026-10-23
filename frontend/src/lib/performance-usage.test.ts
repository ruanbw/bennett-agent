import { describe, expect, it } from 'vitest';

import {
  DEFAULT_PERFORMANCE_USAGE,
  PERFORMANCE_USAGE_OPTIONS,
  PERFORMANCE_USAGE_STORAGE_KEY,
  normalizePerformanceUsage,
  performanceUsageFromStorageEvent,
  readPerformanceUsage,
  writePerformanceUsage,
  type PerformanceUsageStorage,
} from './performance-usage';

/** 内存版 localStorage；可选地让 getItem/setItem 抛异常，模拟隐私模式。 */
function fakeStorage(initial: Record<string, string> = {}, options: { throwOnGet?: boolean; throwOnSet?: boolean } = {}) {
  const data = { ...initial };
  const writes: string[] = [];
  const storage: PerformanceUsageStorage = {
    getItem(key) {
      if (options.throwOnGet) throw new Error('SecurityError');
      return key in data ? data[key] : null;
    },
    setItem(key, value) {
      if (options.throwOnSet) throw new Error('QuotaExceededError');
      writes.push(`${key}=${value}`);
      data[key] = value;
    },
  };
  return { storage, data, writes };
}

describe('normalizePerformanceUsage', () => {
  it('只认两个合法值，其余一律回落默认', () => {
    expect(normalizePerformanceUsage('compact')).toBe('compact');
    expect(normalizePerformanceUsage('detailed')).toBe('detailed');
    expect(normalizePerformanceUsage('')).toBe(DEFAULT_PERFORMANCE_USAGE);
    expect(normalizePerformanceUsage(null)).toBe(DEFAULT_PERFORMANCE_USAGE);
    expect(normalizePerformanceUsage(undefined)).toBe(DEFAULT_PERFORMANCE_USAGE);
    expect(normalizePerformanceUsage('normal')).toBe(DEFAULT_PERFORMANCE_USAGE);
    expect(normalizePerformanceUsage(1)).toBe(DEFAULT_PERFORMANCE_USAGE);
  });

  it('默认是详细：这些数字是「这一轮花了多少」的唯一证据', () => {
    expect(DEFAULT_PERFORMANCE_USAGE).toBe('detailed');
  });
});

describe('readPerformanceUsage / writePerformanceUsage', () => {
  it('没有存储时返回默认（SSR 与隐私模式都要能用）', () => {
    expect(readPerformanceUsage(null)).toBe(DEFAULT_PERFORMANCE_USAGE);
  });

  it('写进去的值能原样读回，且用的就是约定的键', () => {
    const { storage, data } = fakeStorage();
    writePerformanceUsage('compact', storage);

    expect(data[PERFORMANCE_USAGE_STORAGE_KEY]).toBe('compact');
    expect(readPerformanceUsage(storage)).toBe('compact');
  });

  it('键里存了脏值时读成默认，而不是把脏值带进界面', () => {
    const { storage } = fakeStorage({ [PERFORMANCE_USAGE_STORAGE_KEY]: 'verbose' });
    expect(readPerformanceUsage(storage)).toBe(DEFAULT_PERFORMANCE_USAGE);
  });

  it('存储抛异常不影响主流程：读回落默认，写静默失败', () => {
    const readOnly = fakeStorage({}, { throwOnGet: true });
    expect(readPerformanceUsage(readOnly.storage)).toBe(DEFAULT_PERFORMANCE_USAGE);

    const full = fakeStorage({}, { throwOnSet: true });
    expect(() => writePerformanceUsage('compact', full.storage)).not.toThrow();
  });
});

describe('performanceUsageFromStorageEvent', () => {
  it('只处理本键事件，其它键返回 null 让调用方忽略', () => {
    expect(
      performanceUsageFromStorageEvent({ key: 'theme', newValue: 'dark' })
    ).toBeNull();
    expect(
      performanceUsageFromStorageEvent({ key: PERFORMANCE_USAGE_STORAGE_KEY, newValue: 'compact' })
    ).toBe('compact');
  });

  it('clear() 把 key 置空时同样回落默认', () => {
    expect(performanceUsageFromStorageEvent({ key: null, newValue: null })).toBe(
      DEFAULT_PERFORMANCE_USAGE
    );
  });

  it('新值是脏值也回落默认', () => {
    expect(
      performanceUsageFromStorageEvent({ key: PERFORMANCE_USAGE_STORAGE_KEY, newValue: 'huge' })
    ).toBe(DEFAULT_PERFORMANCE_USAGE);
  });
});

describe('档位元数据', () => {
  it('两个档位都有可读的标签与说明，顺序是简洁在前', () => {
    expect(PERFORMANCE_USAGE_OPTIONS.map((option) => option.value)).toEqual(['compact', 'detailed']);
    for (const option of PERFORMANCE_USAGE_OPTIONS) {
      expect(option.label.length).toBeGreaterThan(0);
      expect(option.description.length).toBeGreaterThan(0);
    }
  });
});
