/**
 * 权限选择器的纯逻辑：目录兜底、预设推导、localStorage 持久化。
 *
 * 服务端 `/api/capabilities` 返回的目录是唯一事实来源；这里的常量只在接口还没回来
 * （或老服务端不返回）时提供可渲染的兜底。键名必须与 `app/runtime/permissions.py`
 * 完全一致——请求体原样回传，服务端会拒绝未知键。
 */

import type { PermissionOption, PermissionPreset } from '../types';

export type PermissionMode = 'ask' | 'standard' | 'auto' | 'custom';

export interface PermissionSelection {
  mode: PermissionMode;
  /** 已授予的权限键（按目录顺序去重）。 */
  permissions: string[];
}

export interface PermissionStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

/** 与后端 `DEFAULT_PERMISSION_KEYS` 对齐：只读 + 联网。 */
export const FALLBACK_DEFAULT_PERMISSIONS: readonly string[] = ['read', 'network'];

/** 后端不可达时的兜底目录；字段与 `PermissionOption` 一致。 */
export const FALLBACK_PERMISSION_CATALOG: readonly PermissionOption[] = [
  {
    key: 'read',
    label: '读取数据',
    description: '读取工作区文件、搜索结果等只读内容，不会修改任何东西',
    risk: 'low',
  },
  {
    key: 'write',
    label: '写入文件',
    description: '创建、修改或删除隔离环境中的文件，删除后无法恢复',
    risk: 'medium',
  },
  {
    key: 'execute',
    label: '执行命令',
    description: '在隔离环境中运行 shell 命令、脚本或安装依赖；命令本身可以产生任意副作用',
    risk: 'medium',
  },
  {
    key: 'network',
    label: '联网访问',
    description: '访问互联网、调用搜索引擎或外部网络接口',
    risk: 'low',
  },
  {
    key: 'external',
    label: '外部工具',
    description: '调用 MCP 第三方工具；行为与数据流向由外部 server 决定',
    risk: 'medium',
  },
  {
    key: 'manage',
    label: '环境管理',
    description: '重置或销毁隔离执行环境；已安装的依赖与工作区文件都会丢失',
    risk: 'high',
  },
  {
    key: 'dangerous',
    label: '高风险操作',
    description: '不可恢复的破坏性操作；开启后此类调用不再逐次询问，请只在隔离环境中使用',
    risk: 'high',
  },
];

/** 与后端 `PERMISSION_PRESETS` 对齐；`custom` 由任意勾选组合派生，不在预设里。 */
export const FALLBACK_PERMISSION_PRESETS: readonly PermissionPreset[] = [
  {
    key: 'ask',
    label: '每次询问',
    description: '只读与联网自动执行；写入、执行命令、外部工具逐次询问',
    permissions: ['read', 'network'],
  },
  {
    key: 'standard',
    label: '标准',
    description: '读取、写入与联网自动执行；执行命令与外部工具逐次询问',
    permissions: ['read', 'write', 'network'],
  },
  {
    key: 'auto',
    label: '自动执行',
    description: '除环境管理与高风险操作外全部自动执行；首次使用建议先看一遍工具轨迹',
    permissions: ['read', 'write', 'execute', 'network', 'external'],
  },
];

/** localStorage 的键前缀；带版本号，将来目录改版不会读到旧形状。 */
export const PERMISSION_STORAGE_PREFIX = 'bennett.permissions.v1.';

const VALID_MODES: readonly PermissionMode[] = ['ask', 'standard', 'auto', 'custom'];

export function permissionStorageKey(sessionId: string): string {
  return `${PERMISSION_STORAGE_PREFIX}${sessionId}`;
}

/** 浏览器 localStorage 的安全包装：隐私模式/SSR/测试环境返回 null。 */
export function browserPermissionStorage(): PermissionStorage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

/**
 * 权限存储：localStorage 可用就用它，否则退回进程内 Map。
 *
 * 隐私模式/禁用存储时 localStorage 会抛异常。此时不能直接退回默认值——用户刚在
 * 选择器里勾了「执行命令」，发送时却读到默认授权，看起来像选择器失灵。内存兜底
 * 至少保证当前标签页内一致；刷新后丢失是这种环境可接受的降级。
 */
const MEMORY_PERMISSIONS = new Map<string, string>();

export function sessionPermissionStorage(): PermissionStorage {
  const browser = browserPermissionStorage();
  return {
    getItem: (key) => {
      if (browser) {
        try {
          const value = browser.getItem(key);
          if (value !== null) return value;
        } catch {
          /* 读失败时继续看内存兜底 */
        }
      }
      return MEMORY_PERMISSIONS.get(key) ?? null;
    },
    setItem: (key, value) => {
      MEMORY_PERMISSIONS.set(key, value);
      if (!browser) return;
      try {
        browser.setItem(key, value);
      } catch {
        /* 配额满/隐私模式：内存里已经有一份，当前标签页仍一致 */
      }
    },
  };
}

/** 把服务端目录收窄成可渲染形状；空/非法时退回内置目录。 */
export function normalizeCatalog(value: unknown): PermissionOption[] {
  if (!Array.isArray(value)) return [...FALLBACK_PERMISSION_CATALOG];
  const options: PermissionOption[] = [];
  for (const entry of value) {
    if (!entry || typeof entry !== 'object') continue;
    const { key, label, description, risk } = entry as Partial<PermissionOption>;
    if (typeof key !== 'string' || !key) continue;
    if (typeof label !== 'string' || !label) continue;
    options.push({
      key,
      label,
      description: typeof description === 'string' ? description : '',
      risk: typeof risk === 'string' ? risk : 'low',
    });
  }
  return options.length > 0 ? options : [...FALLBACK_PERMISSION_CATALOG];
}

/** 把服务端预设收窄；只保留目录里存在的权限键。 */
export function normalizePresets(value: unknown, catalog: readonly PermissionOption[]): PermissionPreset[] {
  const valid = new Set(catalog.map((option) => option.key));
  const source = Array.isArray(value) && value.length > 0 ? value : FALLBACK_PERMISSION_PRESETS;
  const presets: PermissionPreset[] = [];
  for (const entry of source) {
    if (!entry || typeof entry !== 'object') continue;
    const { key, label, description, permissions } = entry as Partial<PermissionPreset>;
    if (typeof key !== 'string' || !key) continue;
    if (typeof label !== 'string' || !label) continue;
    presets.push({
      key,
      label,
      description: typeof description === 'string' ? description : '',
      permissions: Array.isArray(permissions)
        ? permissions.filter((permission): permission is string => typeof permission === 'string' && valid.has(permission))
        : [],
    });
  }
  return presets.length > 0 ? presets : [...FALLBACK_PERMISSION_PRESETS];
}

/** 按目录顺序去重并丢掉未知键；保持「显示什么就发送什么」。 */
export function sanitizePermissionList(
  values: unknown,
  catalog: readonly PermissionOption[],
): string[] {
  if (!Array.isArray(values)) return [];
  const valid = new Set(catalog.map((option) => option.key));
  const order = new Map(catalog.map((option, index) => [option.key, index]));
  const seen = new Set<string>();
  const result: string[] = [];
  for (const value of values) {
    if (typeof value !== 'string' || !valid.has(value) || seen.has(value)) continue;
    seen.add(value);
    result.push(value);
  }
  // 目录顺序：后端预设与 UI 的勾选行保持同一个视觉顺序，避免「同一组权限两种排列」
  result.sort((a, b) => (order.get(a) ?? 0) - (order.get(b) ?? 0));
  return result;
}

export function defaultSelection(defaultPermissions: readonly string[] = FALLBACK_DEFAULT_PERMISSIONS): PermissionSelection {
  return {
    mode: 'ask',
    permissions: [...defaultPermissions],
  };
}

/** 当前权限集合等于哪个预设；不等于任何预设时返回 null（即自定义）。 */
export function matchingPreset(
  permissions: readonly string[],
  presets: readonly PermissionPreset[],
): PermissionPreset | null {
  const current = new Set(permissions);
  for (const preset of presets) {
    if (preset.permissions.length !== current.size) continue;
    if (preset.permissions.every((key) => current.has(key))) return preset;
  }
  return null;
}

/** 由权限集合反推模式；显式 custom 时保留自定义。 */
export function inferMode(
  permissions: readonly string[],
  presets: readonly PermissionPreset[],
): PermissionMode {
  const preset = matchingPreset(permissions, presets);
  return (preset?.key as PermissionMode) ?? 'custom';
}

/** 切换到一个预设档位。 */
export function selectionForPreset(
  mode: PermissionMode,
  presets: readonly PermissionPreset[],
  defaultPermissions: readonly string[] = FALLBACK_DEFAULT_PERMISSIONS,
): PermissionSelection {
  if (mode === 'custom') return { mode: 'custom', permissions: [...defaultPermissions] };
  const preset = presets.find((candidate) => candidate.key === mode);
  if (!preset) return defaultSelection(defaultPermissions);
  return { mode, permissions: [...preset.permissions] };
}

/** 勾选/取消一个权限；任何手动改动都进入自定义模式。 */
export function togglePermission(
  selection: PermissionSelection,
  key: string,
  presets: readonly PermissionPreset[],
  catalog: readonly PermissionOption[] = FALLBACK_PERMISSION_CATALOG,
): PermissionSelection {
  const next = new Set(selection.permissions);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  // 按目录顺序产出：预设与自定义列表共用同一个视觉顺序，不和目录打架。
  const permissions = catalog.filter((option) => next.has(option.key)).map((option) => option.key);
  // 未知键（理论上已被 sanitize 清掉）不在这里补回；权限必须是目录里声明过的。
  return { mode: inferMode(permissions, presets), permissions };
}

/** 触发器的第二段文字：预设名，或「自定义」。 */
export function selectionLabel(
  selection: PermissionSelection,
  presets: readonly PermissionPreset[],
): string {
  if (selection.mode === 'custom') {
    return `自定义（${selection.permissions.length}）`;
  }
  return presets.find((preset) => preset.key === selection.mode)?.label ?? '每次询问';
}

export function permissionLabel(key: string, catalog: readonly PermissionOption[]): string {
  return catalog.find((option) => option.key === key)?.label ?? key;
}

export function permissionLabels(keys: readonly string[], catalog: readonly PermissionOption[]): string[] {
  return keys.map((key) => permissionLabel(key, catalog));
}

/**
 * 读取会话权限。**任何解析失败都退回默认值**：权限是安全配置，读坏了应当是「更保守」
 * （默认只读+联网），而不是「更宽松」或抛错让输入框不可用。
 */
export function readSessionPermissions(
  storage: PermissionStorage | null,
  sessionId: string,
  catalog: readonly PermissionOption[] = FALLBACK_PERMISSION_CATALOG,
  presets: readonly PermissionPreset[] = FALLBACK_PERMISSION_PRESETS,
  defaultPermissions: readonly string[] = FALLBACK_DEFAULT_PERMISSIONS,
): PermissionSelection {
  if (!storage || !sessionId) return defaultSelection(defaultPermissions);
  let raw: string | null = null;
  try {
    raw = storage.getItem(permissionStorageKey(sessionId));
  } catch {
    return defaultSelection(defaultPermissions);
  }
  if (!raw) return defaultSelection(defaultPermissions);
  try {
    const parsed = JSON.parse(raw) as Partial<PermissionSelection> | null;
    const permissions = sanitizePermissionList(parsed?.permissions, catalog);
    const mode = VALID_MODES.includes(parsed?.mode as PermissionMode)
      ? (parsed?.mode as PermissionMode)
      : inferMode(permissions, presets);
    return { mode: mode === 'custom' ? 'custom' : inferMode(permissions, presets), permissions };
  } catch {
    return defaultSelection(defaultPermissions);
  }
}

export function writeSessionPermissions(
  storage: PermissionStorage | null,
  sessionId: string,
  selection: PermissionSelection,
): void {
  if (!storage || !sessionId) return;
  try {
    storage.setItem(
      permissionStorageKey(sessionId),
      JSON.stringify({ mode: selection.mode, permissions: selection.permissions }),
    );
  } catch {
    /* localStorage 满了/隐私模式：权限仍在本组件内存里生效，静默失败不打扰用户 */
  }
}

/** 审批卡「本次会话允许」：把这次调用需要的权限并入当前会话。 */
export function addSessionPermissions(
  storage: PermissionStorage | null,
  sessionId: string,
  keys: readonly string[],
  catalog: readonly PermissionOption[] = FALLBACK_PERMISSION_CATALOG,
  presets: readonly PermissionPreset[] = FALLBACK_PERMISSION_PRESETS,
  defaultPermissions: readonly string[] = FALLBACK_DEFAULT_PERMISSIONS,
): PermissionSelection {
  const current = readSessionPermissions(storage, sessionId, catalog, presets, defaultPermissions);
  const merged = sanitizePermissionList([...current.permissions, ...keys], catalog);
  const selection: PermissionSelection = { mode: inferMode(merged, presets), permissions: merged };
  writeSessionPermissions(storage, sessionId, selection);
  return selection;
}
