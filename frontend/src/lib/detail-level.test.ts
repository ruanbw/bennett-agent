import { describe, expect, it } from 'vitest';

import {
  COLLAPSE_TIMING_OPTIONS,
  COLLAPSE_TIMING_STORAGE_KEY,
  DEFAULT_COLLAPSE_TIMING,
  DEFAULT_DETAIL_LEVEL,
  DETAIL_LEVEL_OPTIONS,
  DETAIL_LEVEL_STORAGE_KEY,
  collapseTimingFromStorageEvent,
  detailLevelFromStorageEvent,
  normalizeCollapseTiming,
  normalizeDetailLevel,
  presentationPolicyFor,
  readCollapseTiming,
  readDetailLevel,
  writeCollapseTiming,
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
  it('默认是标准：过程收起，但那一行要带得上动作详情', () => {
    expect(DEFAULT_DETAIL_LEVEL).toBe('standard');
    expect(normalizeDetailLevel(null)).toBe('standard');
    expect(normalizeDetailLevel(undefined)).toBe('standard');
    expect(normalizeDetailLevel('')).toBe('standard');
  });

  it('四档白名单', () => {
    expect(normalizeDetailLevel('compact')).toBe('compact');
    expect(normalizeDetailLevel('standard')).toBe('standard');
    expect(normalizeDetailLevel('detailed')).toBe('detailed');
    expect(normalizeDetailLevel('verbose')).toBe('verbose');
  });

  it('脏值一律回落默认，界面不会停在四档之外', () => {
    expect(normalizeDetailLevel('Compact')).toBe('standard');
    expect(normalizeDetailLevel('DETAILED')).toBe('standard');
    expect(normalizeDetailLevel(1)).toBe('standard');
    expect(normalizeDetailLevel({})).toBe('standard');
    expect(normalizeDetailLevel('ultra')).toBe('standard');
  });

  it('老的两档存储值读成新档位：concise → standard，detailed 保持', () => {
    // concise 当年是「只露一行摘要」。新的 compact 会把那一行里的动作详情也砍掉，
    // 比用户当初选的更弱，所以映射到 standard。
    expect(normalizeDetailLevel('concise')).toBe('standard');
    expect(normalizeDetailLevel('detailed')).toBe('detailed');
  });

  it('DSH 自己的历史值也认：normal / expanded → detailed', () => {
    expect(normalizeDetailLevel('normal')).toBe('detailed');
    expect(normalizeDetailLevel('expanded')).toBe('detailed');
  });
});

describe('收起时机的归一化', () => {
  it('默认回答结束后立即收起', () => {
    expect(DEFAULT_COLLAPSE_TIMING).toBe('completion');
    expect(normalizeCollapseTiming(null)).toBe('completion');
    expect(normalizeCollapseTiming(undefined)).toBe('completion');
    expect(normalizeCollapseTiming('')).toBe('completion');
  });

  it('只接受两档，脏值回落默认', () => {
    expect(normalizeCollapseTiming('completion')).toBe('completion');
    expect(normalizeCollapseTiming('next-input')).toBe('next-input');
    expect(normalizeCollapseTiming('nextInput')).toBe('completion');
    expect(normalizeCollapseTiming(1)).toBe('completion');
  });
});

describe('读写偏好', () => {
  it('写进去再读出来是同一个值', () => {
    const storage = memoryStorage();
    writeDetailLevel('verbose', storage);
    expect(storage.data[DETAIL_LEVEL_STORAGE_KEY]).toBe('verbose');
    expect(readDetailLevel(storage)).toBe('verbose');
  });

  it('收起时机独立存储：改档位不会连带改写收起时机', () => {
    const storage = memoryStorage();
    writeDetailLevel('compact', storage);
    writeCollapseTiming('next-input', storage);

    expect(storage.data[DETAIL_LEVEL_STORAGE_KEY]).toBe('compact');
    expect(storage.data[COLLAPSE_TIMING_STORAGE_KEY]).toBe('next-input');
    expect(readDetailLevel(storage)).toBe('compact');
    expect(readCollapseTiming(storage)).toBe('next-input');
  });

  it('没有存储时读默认值、写是空操作，不抛', () => {
    expect(readDetailLevel(null)).toBe('standard');
    expect(readCollapseTiming(null)).toBe('completion');
    expect(() => writeDetailLevel('verbose', null)).not.toThrow();
    expect(() => writeCollapseTiming('next-input', null)).not.toThrow();
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
    expect(readDetailLevel(throwing)).toBe('standard');
    expect(readCollapseTiming(throwing)).toBe('completion');
    expect(() => writeDetailLevel('verbose', throwing)).not.toThrow();
    expect(() => writeCollapseTiming('next-input', throwing)).not.toThrow();
  });
});

describe('storage 事件（多标签页同步）', () => {
  it('本键的变更被识别', () => {
    expect(detailLevelFromStorageEvent({ key: DETAIL_LEVEL_STORAGE_KEY, newValue: 'verbose' })).toBe(
      'verbose'
    );
    expect(
      collapseTimingFromStorageEvent({ key: COLLAPSE_TIMING_STORAGE_KEY, newValue: 'next-input' })
    ).toBe('next-input');
  });

  it('两个键互不串台：改档位不该被当成改收起时机', () => {
    expect(
      collapseTimingFromStorageEvent({ key: DETAIL_LEVEL_STORAGE_KEY, newValue: 'verbose' })
    ).toBeNull();
    expect(
      detailLevelFromStorageEvent({ key: COLLAPSE_TIMING_STORAGE_KEY, newValue: 'next-input' })
    ).toBeNull();
  });

  it('别的键返回 null，调用方据此忽略', () => {
    expect(detailLevelFromStorageEvent({ key: 'theme_preference', newValue: 'dark' })).toBeNull();
  });

  it('storage.clear() 时回落默认，而不是停在旧值', () => {
    expect(detailLevelFromStorageEvent({ key: null, newValue: null })).toBe('standard');
    expect(collapseTimingFromStorageEvent({ key: null, newValue: null })).toBe('completion');
  });
});

describe('四档的行为策略表', () => {
  it('简洁：折叠已完成轮次，过程组收起，运行中不带详情，已结束思考无预览', () => {
    const policy = presentationPolicyFor('compact');
    expect(policy.foldCompletedTurns).toBe(true);
    expect(policy.stepGrouping).toBe('collapsed');
    expect(policy.liveProcessDetail).toBe(false);
    expect(policy.settledReasoningPreview).toBe(false);
  });

  it('标准：与简洁只差两处——运行中带详情、思考带预览', () => {
    const policy = presentationPolicyFor('standard');
    expect(policy.foldCompletedTurns).toBe(true);
    expect(policy.stepGrouping).toBe('collapsed');
    expect(policy.liveProcessDetail).toBe(true);
    expect(policy.settledReasoningPreview).toBe(true);
  });

  it('详细：折叠已完成轮次，但历史轮次的过程组分组成块并默认展开', () => {
    const policy = presentationPolicyFor('detailed');
    expect(policy.foldCompletedTurns).toBe(true);
    expect(policy.stepGrouping).toBe('history');
    expect(policy.liveProcessDetail).toBe(true);
    expect(policy.settledReasoningPreview).toBe(true);
  });

  it('完全展开：不再折叠已完成轮次，过程也不分组成折叠块', () => {
    const policy = presentationPolicyFor('verbose');
    expect(policy.foldCompletedTurns).toBe(false);
    expect(policy.stepGrouping).toBe('none');
    expect(policy.settledReasoningPreview).toBe(true);
  });

  it('收起时机只挂到策略上，不改变任何档位的分组/折叠行为', () => {
    for (const level of ['compact', 'standard', 'detailed', 'verbose'] as const) {
      const immediate = presentationPolicyFor(level, 'completion');
      const deferred = presentationPolicyFor(level, 'next-input');
      expect(deferred.collapseTiming).toBe('next-input');
      expect(immediate.collapseTiming).toBe('completion');
      // 除了 collapseTiming 之外完全一致
      expect({ ...deferred, collapseTiming: 'completion' }).toEqual(immediate);
    }
  });

  it('同一对入参返回同一个对象引用（调用方可安全比较引用）', () => {
    expect(presentationPolicyFor('detailed', 'next-input')).toBe(
      presentationPolicyFor('detailed', 'next-input')
    );
    expect(presentationPolicyFor('compact', 'completion')).not.toBe(
      presentationPolicyFor('standard', 'completion')
    );
  });

  it('默认收起时机为 completion', () => {
    expect(presentationPolicyFor('standard').collapseTiming).toBe('completion');
  });
});

describe('设置面板用的选项元数据', () => {
  it('四档按从少到多排列，文案与 DSH 一致', () => {
    expect(DETAIL_LEVEL_OPTIONS.map((option) => option.value)).toEqual([
      'compact',
      'standard',
      'detailed',
      'verbose',
    ]);
    expect(DETAIL_LEVEL_OPTIONS.map((option) => option.label)).toEqual([
      '简洁',
      '标准',
      '详细',
      '完全展开',
    ]);
    for (const option of DETAIL_LEVEL_OPTIONS) {
      expect(option.description.length).toBeGreaterThan(0);
    }
  });

  it('收起时机两档的文案', () => {
    expect(COLLAPSE_TIMING_OPTIONS.map((option) => option.value)).toEqual([
      'completion',
      'next-input',
    ]);
    expect(COLLAPSE_TIMING_OPTIONS.map((option) => option.label)).toEqual([
      '回答结束后',
      '下次有新消息时',
    ]);
  });
});
