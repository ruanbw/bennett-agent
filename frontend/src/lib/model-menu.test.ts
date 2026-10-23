/**
 * 模型菜单纯逻辑的行为锁。
 *
 * 这些规则（分组顺序、搜索排序、根面板行、等级文案）都没有「会报错」的失败模式，
 * 只有「用户觉得不对劲」的失败模式，所以每一条都在这里钉死一个可观察的结果。
 */

import { describe, expect, it } from 'vitest';

import {
  MODEL_SEARCH_THRESHOLD,
  UNKNOWN_PROVIDER_NAME,
  buildRootRows,
  compareNameMatch,
  cycleIndex,
  effortLabel,
  emptyCatalogText,
  filterModelGroups,
  groupModelsByProvider,
  matchName,
  orderProviderGroups,
  rankByName,
  shouldShowModelSearch,
  triggerLabel,
} from './model-menu';

const model = (id: string, name: string, provider = '默认网关') => ({ id, name, provider });

describe('groupModelsByProvider', () => {
  it('按提供方分组，组与组内模型都保持目录顺序', () => {
    const groups = groupModelsByProvider([
      model('a/1', 'A1', '默认网关'),
      model('b/1', 'B1', '备用网关'),
      model('a/2', 'A2', '默认网关'),
    ]);

    expect(groups.map((group) => group.name)).toEqual(['默认网关', '备用网关']);
    expect(groups[0].models.map((item) => item.id)).toEqual(['a/1', 'a/2']);
    expect(groups[1].models.map((item) => item.id)).toEqual(['b/1']);
  });

  it('provider 为空时退化到 id 前缀（`{slug}/{model_id}`），同路由仍落进同一组', () => {
    const groups = groupModelsByProvider([
      { id: 'auto/x', name: 'X', provider: '' },
      { id: 'auto/y', name: 'Y', provider: null },
      { id: 'kimi/z', name: 'Z' },
    ]);

    expect(groups.map((group) => group.name)).toEqual(['auto', 'kimi']);
  });

  it('既没有 provider 也没有前缀时归到「其它」，不会丢条目', () => {
    const groups = groupModelsByProvider([{ id: 'lonely', name: 'Lonely', provider: '  ' }]);

    expect(groups).toHaveLength(1);
    expect(groups[0].name).toBe(UNKNOWN_PROVIDER_NAME);
    expect(groups[0].models[0].id).toBe('lonely');
  });

  it('空目录返回空数组', () => {
    expect(groupModelsByProvider([])).toEqual([]);
  });
});

describe('orderProviderGroups', () => {
  it('DeepSeek 账号 > 官方 > deepseek > 其余，其余保持目录顺序', () => {
    const ordered = orderProviderGroups([
      { name: '第三方', models: [] },
      { name: 'DeepSeek', models: [] },
      { name: '备用网关', models: [] },
      { name: 'deepseek-official', models: [] },
      { name: 'deepseek-account', models: [] },
    ]);

    expect(ordered.map((group) => group.name)).toEqual([
      'deepseek-account',
      'deepseek-official',
      'DeepSeek',
      '第三方',
      '备用网关',
    ]);
  });

  it('不按字母排序：未知提供方的顺序完全由调用方（=管理员在后台的排序）决定', () => {
    const ordered = orderProviderGroups([
      { name: 'zeta', models: [] },
      { name: 'alpha', models: [] },
    ]);

    expect(ordered.map((group) => group.name)).toEqual(['zeta', 'alpha']);
  });

  it('不修改入参', () => {
    const original = [
      { name: '第三方', models: [] },
      { name: 'deepseek-account', models: [] },
    ];
    orderProviderGroups(original);

    expect(original.map((group) => group.name)).toEqual(['第三方', 'deepseek-account']);
  });
});

describe('matchName', () => {
  it('空查询匹配一切（保留原顺序）', () => {
    expect(matchName('DeepSeek V4.1 Flash', '  ')).toEqual({
      start: 0,
      span: 0,
      contiguous: 0,
      prefix: true,
    });
  });

  it('不区分大小写的顺序子序列：允许跳字符，不允许乱序', () => {
    expect(matchName('DeepSeek V4.1 Flash', 'dsf')).not.toBeNull();
    // 跨过 "DeepSeek " 之后仍按顺序接上：v → 4 → 1 → f
    expect(matchName('DeepSeek V4.1 Flash', 'v41f')).not.toBeNull();
    // 顺序颠倒 = 不命中（否则「搜 deep 却列出 Gemini」这类噪声就会回来）
    expect(matchName('Gemini 3.8 Flash', 'fg')).toBeNull();
    // 字符存在但顺序反了（f 在 4 之后）同样不命中
    expect(matchName('DeepSeek V4.1 Flash', 'f4')).toBeNull();
  });

  it('给出前缀、跨度、连续命中数，供排序使用', () => {
    expect(matchName('DeepSeek V4.1 Flash', 'deep')).toMatchObject({
      start: 0,
      span: 4,
      contiguous: 3,
      prefix: true,
    });
    // 非连续命中：跨度更大、连续次数更少（d@0 / s@4 / f@14 → 跨度 15）
    expect(matchName('DeepSeek V4.1 Flash', 'dsf')).toMatchObject({
      start: 0,
      span: 15,
      contiguous: 0,
      prefix: false,
    });
  });

  it('名称里没有的字符返回 null；空名称只有空查询才匹配', () => {
    expect(matchName('Kimi K3', 'zzz')).toBeNull();
    expect(matchName('', 'k')).toBeNull();
  });

  it('中英混排也按码元逐个匹配', () => {
    expect(matchName('DeepSeek 深度思考', 'ds深')).not.toBeNull();
  });
});

describe('compareNameMatch', () => {
  const base = { start: 0, span: 4, contiguous: 3, prefix: false };

  it('前缀命中永远排在最前', () => {
    expect(compareNameMatch({ ...base, prefix: true }, { ...base, prefix: false })).toBeLessThan(0);
  });

  it('同为前缀时连续命中多的在前，其次跨度窄的在前', () => {
    expect(
      compareNameMatch({ ...base, contiguous: 3, span: 4 }, { ...base, contiguous: 1, span: 6 }),
    ).toBeLessThan(0);
    expect(
      compareNameMatch({ ...base, contiguous: 1, span: 4 }, { ...base, contiguous: 1, span: 6 }),
    ).toBeLessThan(0);
  });

  it('完全同分时返回 0，由目录顺序兜底', () => {
    expect(compareNameMatch(base, { ...base })).toBe(0);
  });
});

describe('rankByName', () => {
  const catalog = [
    model('a/1', 'Kimi K3'),
    model('a/2', 'DeepSeek V4.1 Flash'),
    model('a/3', 'Gemini 3.8 Flash'),
    model('a/4', 'DeepSeek V4.1 Flash Pro'),
  ];

  it('空查询原样返回目录顺序，且是新数组（调用方可以安全排序）', () => {
    const ranked = rankByName(catalog, '   ');

    expect(ranked.map((item) => item.id)).toEqual(['a/1', 'a/2', 'a/3', 'a/4']);
    expect(ranked).not.toBe(catalog);
  });

  it('前缀命中排在没有前缀的命中之前', () => {
    // 'flash' 是 a/2、a/3、a/4 的子串，但只有 a/3 不是前缀……这里用 'deep' 验前缀
    const ranked = rankByName(catalog, 'deep');

    expect(ranked.map((item) => item.id)).toEqual(['a/2', 'a/4']);
  });

  it('不连续输入也能命中：`dsf` → 两条 DeepSeek Flash', () => {
    expect(rankByName(catalog, 'dsf').map((item) => item.id)).toEqual(['a/2', 'a/4']);
  });

  it('同前缀同分时保持目录顺序（稳定）', () => {
    const ranked = rankByName(
      [model('a/1', 'DeepSeek V4'), model('a/2', 'DeepSeek V4'), model('a/3', 'DeepSeek V4')],
      'deepseek',
    );

    expect(ranked.map((item) => item.id)).toEqual(['a/1', 'a/2', 'a/3']);
  });

  it('没有命中返回空数组', () => {
    expect(rankByName(catalog, 'zzz')).toEqual([]);
  });
});

describe('filterModelGroups', () => {
  const groups = orderProviderGroups(
    groupModelsByProvider([
      model('a/1', 'Kimi K3', '默认网关'),
      model('b/1', 'DeepSeek V4.1 Flash', '备用网关'),
      model('a/2', 'Gemini 3.8 Flash', '默认网关'),
    ]),
  );

  it('搜索只影响组内顺序与命中，不改变组的先后', () => {
    const filtered = filterModelGroups(groups, 'flash');

    expect(filtered.map((group) => group.name)).toEqual(['默认网关', '备用网关']);
    expect(filtered[0].models.map((item) => item.id)).toEqual(['a/2']);
    expect(filtered[1].models.map((item) => item.id)).toEqual(['b/1']);
  });

  it('无命中的分组隐藏', () => {
    const filtered = filterModelGroups(groups, 'kimi');

    expect(filtered.map((group) => group.name)).toEqual(['默认网关']);
  });

  it('空查询返回全部组（副本），不排序', () => {
    const filtered = filterModelGroups(groups, '');

    expect(filtered).toHaveLength(2);
    expect(filtered[0].models.map((item) => item.id)).toEqual(['a/1', 'a/2']);
    expect(filtered[0].models).not.toBe(groups[0].models);
  });
});

describe('shouldShowModelSearch', () => {
  it('超过 4 条才显示搜索框', () => {
    expect(MODEL_SEARCH_THRESHOLD).toBe(4);
    expect(shouldShowModelSearch(4)).toBe(false);
    expect(shouldShowModelSearch(5)).toBe(true);
    expect(shouldShowModelSearch(0)).toBe(false);
  });
});

describe('effortLabel', () => {
  it('关闭标记统一显示成「关闭」，其余等级原样显示', () => {
    expect(effortLabel('off')).toBe('关闭');
    expect(effortLabel('none')).toBe('关闭');
    expect(effortLabel('FALSE')).toBe('关闭');
    expect(effortLabel('high')).toBe('high');
    expect(effortLabel('xhigh')).toBe('xhigh');
  });
});

describe('triggerLabel', () => {
  it('有等级时用 ` · ` 连接，没有等级时不留下悬空分隔符', () => {
    expect(triggerLabel('DeepSeek V4.1 Flash', 'high')).toBe('DeepSeek V4.1 Flash · high');
    expect(triggerLabel('DeepSeek V4.1 Flash', null)).toBe('DeepSeek V4.1 Flash');
    expect(triggerLabel('请选择模型', '')).toBe('请选择模型');
  });
});

describe('buildRootRows', () => {
  it('有等级时是模型 + 思考等级两行', () => {
    const rows = buildRootRows({ modelLabel: 'DeepSeek V4.1 Flash', effortLabel: 'high' });

    expect(rows).toEqual([
      { key: 'model', label: '模型', value: 'DeepSeek V4.1 Flash' },
      { key: 'effort', label: '思考等级', value: 'high' },
    ]);
  });

  it('没有等级时只剩模型一行——调用方据此跳过根面板直接进模型列表', () => {
    const rows = buildRootRows({ modelLabel: '请选择模型' });

    expect(rows).toHaveLength(1);
    expect(rows[0].key).toBe('model');
  });

  it('空字符串等级等同于没有等级', () => {
    expect(buildRootRows({ modelLabel: 'X', effortLabel: '' })).toHaveLength(1);
  });
});

describe('cycleIndex', () => {
  it('在范围内循环', () => {
    expect(cycleIndex(0, 1, 3)).toBe(1);
    expect(cycleIndex(2, 1, 3)).toBe(0);
    expect(cycleIndex(0, -1, 3)).toBe(2);
  });

  it('还没高亮任何项时：↓ 落到第一项，↑ 落到最后一项', () => {
    expect(cycleIndex(null, 1, 3)).toBe(0);
    expect(cycleIndex(null, -1, 3)).toBe(2);
  });

  it('空列表返回 -1，调用方不需要再判长度', () => {
    expect(cycleIndex(null, 1, 0)).toBe(-1);
  });

  it('下标越界时按「没有高亮」处理，不会算出负数或抛错', () => {
    expect(cycleIndex(7, 1, 3)).toBe(0);
    expect(cycleIndex(-3, -1, 3)).toBe(2);
  });
});

describe('emptyCatalogText', () => {
  it('有错误时原样显示服务端的说法（重试按钮就在它下面）', () => {
    expect(emptyCatalogText({ modelsError: '模型目录加载失败：网关超时' })).toBe(
      '模型目录加载失败：网关超时',
    );
    // 错误优先于「加载中」：正转着重试的时候也要看得见上一次为什么失败
    expect(emptyCatalogText({ modelsError: '网关超时', loading: true })).toBe('网关超时');
  });

  it('没有错误且正在加载时说「加载中…」，不要说成「暂无可用模型」', () => {
    expect(emptyCatalogText({ loading: true })).toBe('加载中…');
  });

  it('既没错误也不在加载：确实没有可用模型', () => {
    expect(emptyCatalogText({})).toBe('暂无可用模型');
    expect(emptyCatalogText({ modelsError: '   ', loading: false })).toBe('暂无可用模型');
  });
});
