/**
 * 模型菜单的纯逻辑：按提供方分组、按名称搜索并排序、根面板两行（模型 / 思考等级）与文案。
 *
 * ## 为什么抽到 lib 而不是留在组件里
 *
 * 「某个模型搜不到」「顺序看起来莫名其妙」「等级显示成 off」这些 bug 都不会报错，
 * 只会让用户觉得菜单不好用。而组件的视觉与焦点逻辑在 vitest 的 node 环境里跑不了
 * （仓库没有 jsdom），只有把这些规则写成纯函数才能提前锁住。规矩与
 * `model-selection.ts` / `models-response.ts` 一致。
 *
 * 参考实现是 DSH 的 `@deepseek-ai/dsh-client-ui-model-selection`：
 * `ModelSelect.js` 的 `rankByName`（顺序子序列 + 前缀优先 + 得分 + 目录顺序）与
 * `provider-order.js`（DeepSeek 账号 / 官方排最前，其余保持目录顺序）。
 *
 * ## 本项目的差异
 *
 * DSH 的提供方有稳定的 `id`（`deepseek-account` 等），Bennett 的 `/api/models` 只给
 * `provider` **展示名**（如「默认网关」），且模型 id 形如 `{slug}/{model_id}`。因此
 * 这里按展示名分组、排序只对已知名字生效，其余保持目录顺序——目录顺序就是管理员在
 * 后台排的顺序，比按名字猜更可信。
 */

import { isOff } from './reasoning-effort';

/** 菜单里可用的模型条目（只依赖菜单需要的字段，便于测试里造数据）。 */
export interface ModelMenuModel {
  id: string;
  name: string;
  provider?: string | null;
}

/** 一个提供方分组；`models` 保持目录顺序（排序是 `filterModelGroups` 的事）。 */
export interface ModelMenuGroup<T extends ModelMenuModel = ModelMenuModel> {
  name: string;
  models: T[];
}

/**
 * 目录超过这么多条才显示搜索框。
 *
 * 与 DSH 一致（4）：4 条以内按两下 ↓ 就到了，多一个输入框反而挡住模型名。
 */
export const MODEL_SEARCH_THRESHOLD = 4;

/** `provider` 缺失（老网关路径、条目被裁剪）时的兜底分组名。 */
export const UNKNOWN_PROVIDER_NAME = '其它';

/**
 * 已知提供方的展示优先级：账号第一、官方第二、光秃秃的 DeepSeek 第三。
 *
 * 只是「已知名字往前放」，不在表里的提供方按目录顺序排在后面，不做字母排序——
 * 管理员在后台排好的顺序不该被前端推翻。
 */
export const PROVIDER_PRIORITY: readonly string[] = [
  'deepseek-account',
  'deepseek-official',
  'deepseek',
];

function providerKeyOf(model: ModelMenuModel): string {
  const provider = model.provider?.trim();
  if (provider) return provider;

  // 老网关路径的 provider 可能为空：退化到 id 的前缀（`{slug}/{model_id}`），
  // 至少同一路由的模型还能落进同一组，而不是全挤进「其它」。
  const slash = model.id.indexOf('/');
  return slash > 0 ? model.id.slice(0, slash) : UNKNOWN_PROVIDER_NAME;
}

/** 按提供方分组，保持各组与其内部模型的目录顺序（首次出现的顺序）。 */
export function groupModelsByProvider<T extends ModelMenuModel>(
  models: readonly T[],
): ModelMenuGroup<T>[] {
  const groups = new Map<string, T[]>();

  for (const model of models) {
    const name = providerKeyOf(model);
    const bucket = groups.get(name);
    if (bucket) bucket.push(model);
    else groups.set(name, [model]);
  }

  return [...groups].map(([name, items]) => ({ name, models: items }));
}

/** 已知提供方提前；同优先级之间保持目录顺序（`sort` 是稳定排序）。 */
export function orderProviderGroups<T extends ModelMenuModel>(
  groups: readonly ModelMenuGroup<T>[],
): ModelMenuGroup<T>[] {
  const rankOf = (name: string): number => {
    const index = PROVIDER_PRIORITY.indexOf(name.trim().toLowerCase());
    return index < 0 ? PROVIDER_PRIORITY.length : index;
  };

  return [...groups].sort((left, right) => rankOf(left.name) - rankOf(right.name));
}

/**
 * 一次名称匹配的「质量」。
 *
 * 逐项对应 DSH 的排序规则：前缀命中优先（`prefix`），其次匹配得分（`contiguous`：
 * 连续命中的字符对数，越多说明越不像「跳着找」；`span`：首末命中之间的跨度，越窄
 * 越相关），最后才看目录顺序。
 */
export interface NameMatch {
  /** 首个命中字符的位置：同分时越靠前越可能是用户要找的。 */
  start: number;
  /** 首末命中之间的跨度（`末位置 - 首位置 + 1`）；空查询为 0。 */
  span: number;
  /** 相邻命中（后一个正好在前一个的下一格）的次数。 */
  contiguous: number;
  /** 查询是否是名称的前缀。 */
  prefix: boolean;
}

/**
 * 不区分大小写的「顺序子序列」匹配：查询的每个字符必须按顺序出现，可以不相邻。
 *
 * 比子串匹配宽松（`v41f` 能命中 `V4.1 Flash`），但比任意字符集严格（顺序不能乱），
 * 因此不会出现「搜 deep 却列出 Gemini」的噪声。空查询视为匹配一切（保留原顺序）。
 */
export function matchName(name: string, query: string): NameMatch | null {
  const haystack = name.trim().toLowerCase();
  const needle = query.trim().toLowerCase();

  if (!needle) return { start: 0, span: 0, contiguous: 0, prefix: true };
  if (!haystack) return null;

  let cursor = 0;
  let start = -1;
  let last = -1;
  let contiguous = 0;

  // 按 UTF-16 码元逐个比较，和 indexOf 的坐标一致（for...of 按码点迭代会错位）。
  for (let index = 0; index < needle.length; index += 1) {
    const found = haystack.indexOf(needle[index], cursor);
    if (found < 0) return null;

    if (start < 0) {
      start = found;
    } else if (found === last + 1) {
      contiguous += 1;
    }

    last = found;
    cursor = found + 1;
  }

  return {
    start,
    span: last - start + 1,
    contiguous,
    prefix: haystack.startsWith(needle),
  };
}

/** 排序比较器：返回负数表示 `left` 更相关。 */
export function compareNameMatch(left: NameMatch, right: NameMatch): number {
  if (left.prefix !== right.prefix) return left.prefix ? -1 : 1;
  if (left.contiguous !== right.contiguous) return right.contiguous - left.contiguous;
  if (left.span !== right.span) return left.span - right.span;
  return left.start - right.start;
}

/**
 * 按名称把模型排一遍：前缀优先 → 匹配得分 → 目录顺序。
 *
 * 空查询返回目录顺序的副本（不排序），因为「没搜索」时用户期待的是熟悉的顺序。
 */
export function rankByName<T extends { name: string }>(
  models: readonly T[],
  query: string,
): T[] {
  const needle = query.trim();
  if (!needle) return [...models];

  const scored: Array<{ model: T; match: NameMatch; index: number }> = [];
  models.forEach((model, index) => {
    const match = matchName(model.name, needle);
    if (match) scored.push({ model, match, index });
  });

  scored.sort(
    (left, right) => compareNameMatch(left.match, right.match) || left.index - right.index,
  );

  return scored.map((entry) => entry.model);
}

/**
 * 逐组过滤并排序，丢掉没有命中的分组。
 *
 * 组顺序由调用方先过 `orderProviderGroups` 决定，这里不重排——搜索只影响组内。
 */
export function filterModelGroups<T extends ModelMenuModel>(
  groups: readonly ModelMenuGroup<T>[],
  query: string,
): ModelMenuGroup<T>[] {
  const needle = query.trim();

  if (!needle) return groups.map((group) => ({ name: group.name, models: [...group.models] }));

  return groups
    .map((group) => ({ name: group.name, models: rankByName(group.models, needle) }))
    .filter((group) => group.models.length > 0);
}

/** 目录超过 4 条才值得给搜索框（见 `MODEL_SEARCH_THRESHOLD`）。 */
export function shouldShowModelSearch(totalModels: number): boolean {
  return totalModels > MODEL_SEARCH_THRESHOLD;
}

/**
 * 思考等级的展示文案：`off`/`none`/`false` 都叫「关闭」。
 *
 * 其余等级原样显示 id（`low` / `medium` / `high`）。`/api/models` 只给等级取值、不给
 * 展示名，前端不能凭空造「低/中/高」——除非哪天契约里真的带了名字。
 */
export function effortLabel(level: string): string {
  return isOff(level) ? '关闭' : level;
}

/** 触发器文字：`模型名 · 思考等级`；没有等级时只有模型名（不留悬空的分隔符）。 */
export function triggerLabel(modelLabel: string, effort?: string | null): string {
  return effort ? `${modelLabel} · ${effort}` : modelLabel;
}

export type RootMenuRowKey = 'model' | 'effort';

/** 根面板的一行：左侧固定标签，右侧当前值。 */
export interface RootMenuRow {
  key: RootMenuRowKey;
  label: string;
  value: string;
}

/**
 * 根面板的两行。
 *
 * 只有模型一行时**不要**渲染根面板：任务要求「模型不支持思考」时点击直接进模型列表，
 * 否则用户会先看到一个只有一行的空壳菜单，还要再点一次。
 */
export function buildRootRows(input: {
  modelLabel: string;
  effortLabel?: string | null;
}): RootMenuRow[] {
  const rows: RootMenuRow[] = [{ key: 'model', label: '模型', value: input.modelLabel }];

  if (input.effortLabel) {
    rows.push({ key: 'effort', label: '思考等级', value: input.effortLabel });
  }

  return rows;
}

/**
 * 在长度 `length` 的列表里循环移动下标（↑/↓ 与焦点移动共用）。
 *
 * `current` 越界或为 `null` 时：向后取第一个、向前取最后一个——与原生 `<select>` 里
 * 「还没聚焦任何项时按 ↓ 到第一项、按 ↑ 到末项」的直觉一致。
 */
export function cycleIndex(
  current: number | null,
  delta: 1 | -1,
  length: number,
): number {
  if (length <= 0) return -1;
  if (current === null || current < 0 || current >= length) {
    return delta > 0 ? 0 : length - 1;
  }
  return (current + delta + length) % length;
}

/**
 * 空目录时面板里那句话。
 *
 * 三种情况给三种结论，因为它们导向的下一步完全不同：
 * - 有错误：把服务端/网关的话原样摆出来（「重试」按钮在它下面）；
 * - 正在加载（首次拉取或用户刚点了重试）：说明它在动，别让人以为已经空了；
 * - 都没有：真的没有可用模型，去后台加。
 */
export function emptyCatalogText(input: {
  modelsError?: string | null;
  loading?: boolean;
}): string {
  const error = input.modelsError?.trim();
  if (error) return error;
  return input.loading ? '加载中…' : '暂无可用模型';
}
