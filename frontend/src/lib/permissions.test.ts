import { describe, expect, it } from 'vitest';

import {
  FALLBACK_DEFAULT_PERMISSIONS,
  FALLBACK_PERMISSION_CATALOG,
  FALLBACK_PERMISSION_PRESETS,
  addSessionPermissions,
  defaultSelection,
  inferMode,
  matchingPreset,
  normalizeCatalog,
  normalizePresets,
  permissionLabel,
  permissionLabels,
  readSessionPermissions,
  sanitizePermissionList,
  selectionForPreset,
  selectionLabel,
  togglePermission,
  writeSessionPermissions,
  type PermissionStorage,
  type PermissionSelection,
} from './permissions';

function memoryStorage(initial: Record<string, string> = {}): PermissionStorage & { data: Record<string, string> } {
  const data = { ...initial };
  return {
    data,
    getItem: (key) => (key in data ? data[key] : null),
    setItem: (key, value) => {
      data[key] = value;
    },
  };
}

describe('权限目录兜底', () => {
  it('服务端目录不可用时退回内置目录，键名与后端一致', () => {
    const catalog = normalizeCatalog(undefined);
    expect(catalog.map((option) => option.key)).toEqual([
      'read',
      'write',
      'execute',
      'network',
      'external',
      'manage',
      'dangerous',
    ]);
  });

  it('服务端返回空数组也按「拿不到」处理，不让选择器变成空白', () => {
    expect(normalizeCatalog([])).toEqual(FALLBACK_PERMISSION_CATALOG);
  });

  it('预设只保留目录里存在的权限键', () => {
    const catalog = normalizeCatalog([
      { key: 'read', label: '读取', description: '', risk: 'low' },
      { key: 'execute', label: '执行', description: '', risk: 'medium' },
    ]);
    const presets = normalizePresets(
      [
        {
          key: 'ask',
          label: '每次询问',
          description: '',
          permissions: ['read', 'execute', 'future'],
        },
      ],
      catalog,
    );
    expect(presets[0].permissions).toEqual(['read', 'execute']);
  });
});

describe('选择与预设', () => {
  it('默认是只读 + 联网', () => {
    expect(defaultSelection().permissions).toEqual([...FALLBACK_DEFAULT_PERMISSIONS]);
    expect(defaultSelection().mode).toBe('ask');
  });

  it('能识别当前权限集合对应的预设', () => {
    const standard = selectionForPreset('standard', FALLBACK_PERMISSION_PRESETS);
    expect(matchingPreset(standard.permissions, FALLBACK_PERMISSION_PRESETS)?.key).toBe('standard');
    expect(inferMode(standard.permissions, FALLBACK_PERMISSION_PRESETS)).toBe('standard');
  });

  it('手动勾选切换到自定义模式，并按目录顺序维护集合', () => {
    const initial = defaultSelection();
    const next = togglePermission(initial, 'execute', FALLBACK_PERMISSION_PRESETS);
    expect(next.mode).toBe('custom');
    expect(next.permissions).toEqual(['read', 'execute', 'network']);
    // 再取消刚好回到某个预设的集合时，模式也跟着回到该预设（显示与实际一致）
    const back = togglePermission(next, 'execute', FALLBACK_PERMISSION_PRESETS);
    expect(back.mode).toBe('ask');
    expect(back.permissions).toEqual(['read', 'network']);
  });

  it('取消全部权限也是合法状态（每个调用都要问）', () => {
    let selection: PermissionSelection = defaultSelection();
    for (const key of selection.permissions) {
      selection = togglePermission(selection, key, FALLBACK_PERMISSION_PRESETS);
    }
    expect(selection.permissions).toEqual([]);
    expect(selection.mode).toBe('custom');
  });

  it('触发器文案：预设名或自定义（数量）', () => {
    expect(selectionLabel(defaultSelection(), FALLBACK_PERMISSION_PRESETS)).toBe('每次询问');
    const custom = togglePermission(defaultSelection(), 'execute', FALLBACK_PERMISSION_PRESETS);
    expect(selectionLabel(custom, FALLBACK_PERMISSION_PRESETS)).toBe('自定义（3）');
  });

  it('权限名按目录翻译，未知键原样返回', () => {
    expect(permissionLabel('execute', FALLBACK_PERMISSION_CATALOG)).toBe('执行命令');
    expect(permissionLabels(['read', 'future'], FALLBACK_PERMISSION_CATALOG)).toEqual(['读取数据', 'future']);
  });
});

describe('会话级持久化', () => {
  it('没有存储时返回默认值', () => {
    expect(readSessionPermissions(null, 'sess_a')).toEqual(defaultSelection());
  });

  it('写入后可读回同一个选择', () => {
    const storage = memoryStorage();
    const selection: PermissionSelection = { mode: 'custom', permissions: ['read', 'execute'] };
    writeSessionPermissions(storage, 'sess_a', selection);
    expect(readSessionPermissions(storage, 'sess_a')).toEqual(selection);
  });

  it('不同会话互不影响', () => {
    const storage = memoryStorage();
    writeSessionPermissions(storage, 'sess_a', { mode: 'standard', permissions: ['read', 'write', 'network'] });
    expect(readSessionPermissions(storage, 'sess_b')).toEqual(defaultSelection());
  });

  it('坏 JSON / 未知权限键都退回安全一侧，不抛错', () => {
    const storage = memoryStorage({ 'bennett.permissions.v1.sess_a': '{broken' });
    expect(readSessionPermissions(storage, 'sess_a')).toEqual(defaultSelection());

    storage.data['bennett.permissions.v1.sess_a'] = JSON.stringify({
      mode: 'auto',
      permissions: ['read', 'future_permission'],
    });
    const selection = readSessionPermissions(storage, 'sess_a');
    expect(selection.permissions).toEqual(['read']);
    expect(selection.mode).toBe('custom');
  });

  it('审批卡「本次会话允许」把权限并进当前选择', () => {
    const storage = memoryStorage();
    const after = addSessionPermissions(storage, 'sess_a', ['execute']);
    expect(after.permissions).toEqual(['read', 'execute', 'network']);
    expect(readSessionPermissions(storage, 'sess_a').permissions).toEqual(['read', 'execute', 'network']);
  });

  it('sanitize 会去重并按目录顺序排列', () => {
    expect(sanitizePermissionList(['execute', 'read', 'execute'], FALLBACK_PERMISSION_CATALOG)).toEqual([
      'read',
      'execute',
    ]);
  });
});
