/**
 * 「对话显示」偏好：思考与工具调用在对话里露多少细节。
 *
 * 四档对齐 DSH 的 transcriptView（`/tmp/dshx/dsh-client-ui-chat` 的 presentation-policy）：
 *   - `compact`（简洁）：已完成轮次折叠；过程组收起，运行中的组头**不带**动作详情；
 *     已结束的思考行**不显示**预览（只有「思考」两个字）。
 *   - `standard`（标准）：已完成轮次折叠；过程组收起；运行中的组头带详情
 *     （「正在运行命令 · npm test」）；已结束的思考行带预览。
 *   - `detailed`（详细）：已完成轮次折叠；**历史**轮次的过程组默认保持展开；
 *     运行中带详情，思考带预览。
 *   - `verbose`（完全展开）：不再折叠已完成轮次；过程**不再**收成可折叠的组，
 *     历史过程行直接铺开。
 *
 * 默认 `standard`：多数时候用户只关心答案与「刚才干了什么」这一行，但那一行本身要
 * 可读（有动作详情）。`compact` 是给「只要答案」的人准备的，`verbose` 反之。
 *
 * 另有一个**独立**设置项「工作步骤收起时机」（collapseTiming）：它只决定刚结束的
 * 那一轮什么时候收起来，与上面四档正交——所以是两套存储键、两个状态，而不是把
 * 八种组合塞进一个枚举。
 *
 * 与主题偏好同样的边界：存储在 localStorage，读不到/读到脏值/存储抛异常都回落默认，
 * 不让偏好影响对话本身。本文件不依赖 React，可以在 node 环境的 vitest 里直接测。
 */

export type DetailLevel = 'compact' | 'standard' | 'detailed' | 'verbose';

/** 「工作步骤收起时机」。 */
export type CollapseTiming = 'completion' | 'next-input';

/**
 * 过程组的呈现方式。
 *
 * - `collapsed`：所有过程组都收成可折叠块（简洁 / 标准）；
 * - `history`：只有**历史**（已结束）轮次的过程组是可折叠块，且默认展开（详细）；
 * - `none`：不分组成折叠块，历史过程行直接显示（完全展开）。
 */
export type StepGrouping = 'collapsed' | 'history' | 'none';

/** 一档显示模式解出来的全部行为开关。 */
export interface PresentationPolicy {
  mode: DetailLevel;
  collapseTiming: CollapseTiming;
  /** 已完成轮次是否自动折叠（只有「完全展开」关掉）。 */
  foldCompletedTurns: boolean;
  /** 过程行怎么分组，见 `StepGrouping`。 */
  stepGrouping: StepGrouping;
  /** 运行中的组头是否带当前动作详情（命令、路径、查询词…）。 */
  liveProcessDetail: boolean;
  /** 已结束的思考行是否显示一行预览摘要。 */
  settledReasoningPreview: boolean;
}

/** 默认档位。 */
export const DEFAULT_DETAIL_LEVEL: DetailLevel = 'standard';

/** 默认收起时机：回答结束后立即收起。 */
export const DEFAULT_COLLAPSE_TIMING: CollapseTiming = 'completion';

/** 存储键。 */
export const DETAIL_LEVEL_STORAGE_KEY = 'conversation_detail_level';

/** 收起时机的存储键（与档位分开存：两者正交，改一个不该重写另一个）。 */
export const COLLAPSE_TIMING_STORAGE_KEY = 'conversation_collapse_timing';

/** 四档的行为表，逐一对应 DSH 的 `POLICIES`。 */
const POLICIES: Record<DetailLevel, Omit<PresentationPolicy, 'collapseTiming'>> = {
  compact: {
    mode: 'compact',
    foldCompletedTurns: true,
    stepGrouping: 'collapsed',
    liveProcessDetail: false,
    settledReasoningPreview: false,
  },
  standard: {
    mode: 'standard',
    foldCompletedTurns: true,
    stepGrouping: 'collapsed',
    liveProcessDetail: true,
    settledReasoningPreview: true,
  },
  detailed: {
    mode: 'detailed',
    foldCompletedTurns: true,
    stepGrouping: 'history',
    liveProcessDetail: true,
    settledReasoningPreview: true,
  },
  verbose: {
    mode: 'verbose',
    foldCompletedTurns: false,
    stepGrouping: 'none',
    liveProcessDetail: false,
    settledReasoningPreview: true,
  },
};

/**
 * 档位 + 收起时机 → 行为策略。
 *
 * 八种组合在模块加载时就建好，返回值对同一对入参是**同一个对象**。调用方因此可以
 * 直接比较引用（`useMemo` 依赖、store 快照相等判断），不会因为「值相等但引用不同」
 * 触发无谓重渲染。
 */
const POLICY_TABLE: Record<CollapseTiming, Record<DetailLevel, PresentationPolicy>> = {
  completion: buildTimingTable('completion'),
  'next-input': buildTimingTable('next-input'),
};

function buildTimingTable(collapseTiming: CollapseTiming): Record<DetailLevel, PresentationPolicy> {
  const table = {} as Record<DetailLevel, PresentationPolicy>;
  for (const mode of Object.keys(POLICIES) as DetailLevel[]) {
    table[mode] = { ...POLICIES[mode], collapseTiming };
  }
  return table;
}

export function presentationPolicyFor(
  level: DetailLevel,
  collapseTiming: CollapseTiming = DEFAULT_COLLAPSE_TIMING,
): PresentationPolicy {
  return POLICY_TABLE[collapseTiming][level] ?? POLICY_TABLE[DEFAULT_COLLAPSE_TIMING][DEFAULT_DETAIL_LEVEL];
}

/**
 * 老版本存的是两档（`concise` / `detailed`）。
 *
 * `concise` 读成 `standard`——它当年表示「只露一行过程摘要」，而新的 `compact`
 * 会把那一行里的动作详情也砍掉，比用户当初选的更弱；`detailed` 语义未变。
 * 另外 DSH 自己的历史值 `normal` / `expanded` 也一并认成 `detailed`。
 */
const LEGACY_DETAIL_LEVELS: Record<string, DetailLevel> = {
  concise: 'standard',
  normal: 'detailed',
  expanded: 'detailed',
};

/** 白名单：未知值一律回落默认，避免历史脏值让界面停在四档之外。 */
export function normalizeDetailLevel(value: unknown): DetailLevel {
  if (typeof value !== 'string') return DEFAULT_DETAIL_LEVEL;
  if (value === 'compact' || value === 'standard' || value === 'detailed' || value === 'verbose') {
    return value;
  }
  return LEGACY_DETAIL_LEVELS[value] ?? DEFAULT_DETAIL_LEVEL;
}

/** 收起时机的白名单。 */
export function normalizeCollapseTiming(value: unknown): CollapseTiming {
  return value === 'completion' || value === 'next-input' ? value : DEFAULT_COLLAPSE_TIMING;
}

/** localStorage 的最小接口：只用到 getItem/setItem，方便单测注入假实现。 */
export interface DetailLevelStorage {
  getItem: (key: string) => string | null;
  setItem: (key: string, value: string) => void;
}

/** 取 localStorage；访问本身抛异常时返回 null（与 `theme-context.getThemeStorage` 同口径）。 */
export function getDetailLevelStorage(): DetailLevelStorage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage;
  } catch (err) {
    console.warn('localStorage 不可用，对话显示偏好不会被持久化:', err);
    return null;
  }
}

/** 读偏好：读不到、读到脏值、存储抛异常都回落默认。 */
export function readDetailLevel(
  storage: DetailLevelStorage | null = getDetailLevelStorage()
): DetailLevel {
  if (!storage) return DEFAULT_DETAIL_LEVEL;
  try {
    return normalizeDetailLevel(storage.getItem(DETAIL_LEVEL_STORAGE_KEY));
  } catch (err) {
    console.warn('读取对话显示偏好失败，回落默认:', err);
    return DEFAULT_DETAIL_LEVEL;
  }
}

/** 写偏好：写失败只记日志——模式已经切换生效，不该把存储异常抛给调用方。 */
export function writeDetailLevel(
  level: DetailLevel,
  storage: DetailLevelStorage | null = getDetailLevelStorage()
): void {
  if (!storage) return;
  try {
    storage.setItem(DETAIL_LEVEL_STORAGE_KEY, level);
  } catch (err) {
    console.warn('保存对话显示偏好失败:', err);
  }
}

/** 读收起时机；口径同 `readDetailLevel`。 */
export function readCollapseTiming(
  storage: DetailLevelStorage | null = getDetailLevelStorage()
): CollapseTiming {
  if (!storage) return DEFAULT_COLLAPSE_TIMING;
  try {
    return normalizeCollapseTiming(storage.getItem(COLLAPSE_TIMING_STORAGE_KEY));
  } catch (err) {
    console.warn('读取收起时机偏好失败，回落默认:', err);
    return DEFAULT_COLLAPSE_TIMING;
  }
}

/** 写收起时机；失败只记日志。 */
export function writeCollapseTiming(
  timing: CollapseTiming,
  storage: DetailLevelStorage | null = getDetailLevelStorage()
): void {
  if (!storage) return;
  try {
    storage.setItem(COLLAPSE_TIMING_STORAGE_KEY, timing);
  } catch (err) {
    console.warn('保存收起时机偏好失败:', err);
  }
}

/**
 * `storage` 事件 → 需要的档位；与本键无关的事件返回 null（调用方据此忽略）。
 *
 * `event.key === null` 表示 `storage.clear()`：偏好被清掉，同样回落默认。
 */
export function detailLevelFromStorageEvent(
  event: Pick<StorageEvent, 'key' | 'newValue'>
): DetailLevel | null {
  if (event.key !== null && event.key !== DETAIL_LEVEL_STORAGE_KEY) return null;
  return normalizeDetailLevel(event.newValue);
}

/** `storage` 事件 → 需要的收起时机；与本键无关返回 null。 */
export function collapseTimingFromStorageEvent(
  event: Pick<StorageEvent, 'key' | 'newValue'>
): CollapseTiming | null {
  if (event.key !== null && event.key !== COLLAPSE_TIMING_STORAGE_KEY) return null;
  return normalizeCollapseTiming(event.newValue);
}

/**
 * 档位元数据（文案与顺序）。图标映射留在组件层——`lib` 是纯 TS，不引 React 与图标库。
 *
 * `description` 只描述**默认状态**：四档都能点开看细节，区别只是默认值。
 */
export const DETAIL_LEVEL_OPTIONS = [
  {
    value: 'compact' as const,
    label: '简洁',
    description: '过程收起，只留动作标题',
  },
  {
    value: 'standard' as const,
    label: '标准',
    description: '过程收起，标题带当前详情',
  },
  {
    value: 'detailed' as const,
    label: '详细',
    description: '历史过程默认展开',
  },
  {
    value: 'verbose' as const,
    label: '完全展开',
    description: '过程行不再折叠',
  },
] as const;

/**
 * 收起时机元数据。
 *
 * 「下次有新消息时」对长回答有用：刚跑完那一轮的过程还摊着，用户能立刻回看它
 * 刚才读了哪些文件，而不必再点开一次。
 */
export const COLLAPSE_TIMING_OPTIONS = [
  {
    value: 'completion' as const,
    label: '回答结束后',
    description: '本轮一结束就收起工作步骤',
  },
  {
    value: 'next-input' as const,
    label: '下次有新消息时',
    description: '保留到下一轮开始，便于回看',
  },
] as const;
