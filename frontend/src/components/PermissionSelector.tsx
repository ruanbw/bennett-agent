import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { Check, ChevronDown, ShieldAlert, ShieldCheck, SlidersHorizontal } from 'lucide-react';

import { useChatSessionId } from '@/context/chat-session';
import {
  FALLBACK_DEFAULT_PERMISSIONS,
  sessionPermissionStorage,
  normalizeCatalog,
  normalizePresets,
  readSessionPermissions,
  sanitizePermissionList,
  selectionForPreset,
  selectionLabel,
  togglePermission,
  writeSessionPermissions,
  type PermissionMode,
  type PermissionSelection,
} from '@/lib/permissions';
import type { PermissionOption, PermissionPreset } from '@/types';

export interface PermissionSelectorProps {
  /** 服务端 `/api/capabilities` 返回的权限目录；缺省时使用内置兜底。 */
  catalog?: readonly PermissionOption[];
  presets?: readonly PermissionPreset[];
  /** 服务端默认授权（客户端未传 `permissions` 时服务端实际使用的集合）。 */
  defaultPermissions?: readonly string[];
  disabled?: boolean;
}

/**
 * 输入框里的权限选择器（模型选择器旁边的盾牌按钮）。
 *
 * 交互模型对齐 Claude Code / Cursor 的「运行模式 + 允许清单」组合：
 * - 三档预设（每次询问 / 标准 / 自动执行）覆盖常见取舍，一次点击完成；
 * - 自定义逐项勾选，选项来自服务端目录，不在前端猜工具与权限的映射；
 * - 选择按**会话**存 localStorage，随每条消息发送给服务端；服务端才是执行闸门。
 *
 * 为什么不是「每条消息弹一次权限确认」：那只是另一种审批疲劳。预设让用户在看到任务
 * 之前表达一次意图，Agent 执行时只在越出授权范围时打断——审批发生在真正需要的边界。
 */
export function PermissionSelector({
  catalog,
  presets,
  defaultPermissions,
  disabled = false,
}: PermissionSelectorProps) {
  // 理论上权限选择器只会挂在有会话的对话区里；`null` 时仍渲染，但读写落到空 key 上
  // （`readSessionPermissions` 对空 sessionId 直接返回默认值，不会污染别的会话）。
  const sessionId = useChatSessionId() ?? '';
  const effectiveCatalog = useMemo(() => normalizeCatalog(catalog), [catalog]);
  const effectivePresets = useMemo(
    () => normalizePresets(presets, effectiveCatalog),
    [presets, effectiveCatalog],
  );
  const effectiveDefaults = useMemo(
    () =>
      sanitizePermissionList(
        defaultPermissions && defaultPermissions.length > 0
          ? defaultPermissions
          : FALLBACK_DEFAULT_PERMISSIONS,
        effectiveCatalog,
      ),
    [defaultPermissions, effectiveCatalog],
  );

  const [selection, setSelection] = useState<PermissionSelection>(() =>
    readSessionPermissions(
      sessionPermissionStorage(),
      sessionId,
      effectiveCatalog,
      effectivePresets,
      effectiveDefaults,
    ),
  );
  const [isOpen, setIsOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const id = useId();

  // 切换会话/目录时重读：每个会话有自己的一份权限选择，不能把上个会话的授权带过去。
  // 用渲染期状态调整而不是 effect：effect 会先渲染一帧旧授权，用户能看见选择器闪一下
  // 上一个会话的权限；渲染期修正则不会提交那一帧。
  const selectionKey = `${sessionId}\u0000${effectiveCatalog.map((option) => option.key).join(',')}\u0000${effectivePresets
    .map((preset) => `${preset.key}:${preset.permissions.join('+')}`)
    .join(',')}\u0000${effectiveDefaults.join(',')}`;
  const [loadedSelectionKey, setLoadedSelectionKey] = useState(selectionKey);
  if (loadedSelectionKey !== selectionKey) {
    setLoadedSelectionKey(selectionKey);
    setSelection(
      readSessionPermissions(
        sessionPermissionStorage(),
        sessionId,
        effectiveCatalog,
        effectivePresets,
        effectiveDefaults,
      ),
    );
    setIsOpen(false);
  }

  // 点击外部与 Escape 关闭：菜单是绝对定位的，必须自己处理这两条，否则会一直挂着。
  useEffect(() => {
    if (!isOpen) return undefined;
    const onPointerDown = (event: MouseEvent | TouchEvent) => {
      const target = event.target;
      if (target instanceof Node && containerRef.current?.contains(target)) return;
      setIsOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      setIsOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('touchstart', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('touchstart', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [isOpen]);

  const persist = useCallback(
    (next: PermissionSelection) => {
      setSelection(next);
      writeSessionPermissions(sessionPermissionStorage(), sessionId, next);
    },
    [sessionId],
  );

  const choosePreset = useCallback(
    (mode: PermissionMode) => {
      persist(selectionForPreset(mode, effectivePresets, effectiveDefaults));
      setIsOpen(false);
      triggerRef.current?.focus();
    },
    [persist, effectivePresets, effectiveDefaults],
  );

  const toggle = useCallback(
    (key: string) => {
      persist(togglePermission(selection, key, effectivePresets, effectiveCatalog));
    },
    [persist, selection, effectivePresets, effectiveCatalog],
  );

  const label = selectionLabel(selection, effectivePresets);
  const hasDangerous = selection.permissions.includes('dangerous');
  const shownPresets = effectivePresets.filter((preset) => preset.key !== 'custom');

  return (
    <div ref={containerRef} className="relative inline-block min-w-0">
      <button
        ref={triggerRef}
        type="button"
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={isOpen}
        aria-controls={isOpen ? `${id}-menu` : undefined}
        aria-label={`权限：${label}`}
        onClick={() => setIsOpen((open) => !open)}
        className="flex h-7 min-w-0 cursor-pointer items-center gap-1 rounded-lg px-1.5 text-xs text-muted-foreground transition hover:bg-accent/60 hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50 pointer-coarse:h-9"
        title="选择本会话允许 Agent 使用的权限"
      >
        {hasDangerous ? (
          <ShieldAlert className="size-3.5 shrink-0 text-destructive" aria-hidden="true" />
        ) : selection.mode === 'auto' ? (
          <ShieldCheck className="size-3.5 shrink-0 text-amber-600 dark:text-amber-500" aria-hidden="true" />
        ) : selection.mode === 'custom' ? (
          <SlidersHorizontal className="size-3.5 shrink-0" aria-hidden="true" />
        ) : (
          <ShieldCheck className="size-3.5 shrink-0" aria-hidden="true" />
        )}
        {/* 窄屏只留盾牌图标：模型名本来就长，两个文字触发器会把发送按钮挤到第二行。 */}
        <span className="hidden truncate sm:inline">权限 · {label}</span>
        <ChevronDown className="hidden size-3 shrink-0 opacity-60 sm:block" aria-hidden="true" />
      </button>

      {isOpen && (
        <div
          id={`${id}-menu`}
          role="menu"
          aria-label="选择本会话权限"
          className="absolute bottom-full left-0 z-50 mb-2 max-h-[min(28rem,70vh)] w-80 max-w-[calc(100vw-2rem)] overflow-y-auto rounded-xl border border-border bg-popover p-1.5 text-popover-foreground shadow-lg"
        >
          <p className="px-2 pb-1 pt-1.5 text-[11px] font-medium text-muted-foreground">预设</p>
          {shownPresets.map((preset) => {
            const checked = selection.mode === preset.key;
            return (
              <button
                key={preset.key}
                type="button"
                role="menuitemradio"
                aria-checked={checked}
                onClick={() => choosePreset(preset.key as PermissionMode)}
                className="flex w-full cursor-pointer items-start gap-2 rounded-lg px-2 py-1.5 text-left transition hover:bg-accent/60 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60"
              >
                <span className="mt-0.5 flex size-4 shrink-0 items-center justify-center">
                  {checked && <Check className="size-3.5 text-primary" aria-hidden="true" />}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block text-xs font-medium text-foreground">{preset.label}</span>
                  <span className="mt-0.5 block text-[10.5px] leading-relaxed text-muted-foreground">
                    {preset.description}
                  </span>
                </span>
              </button>
            );
          })}

          <div className="my-1 border-t border-border/70" role="separator" />
          <p className="px-2 pb-1 pt-1 text-[11px] font-medium text-muted-foreground">自定义权限</p>
          {effectiveCatalog.map((option) => {
            const checked = selection.permissions.includes(option.key);
            const highRisk = option.risk === 'high';
            return (
              <button
                key={option.key}
                type="button"
                role="menuitemcheckbox"
                aria-checked={checked}
                onClick={() => toggle(option.key)}
                className="flex w-full cursor-pointer items-start gap-2 rounded-lg px-2 py-1.5 text-left transition hover:bg-accent/60 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60"
              >
                <span
                  aria-hidden="true"
                  className={`mt-0.5 flex size-4 shrink-0 items-center justify-center rounded border ${
                    checked ? 'border-primary bg-primary text-primary-foreground' : 'border-border/80'
                  }`}
                >
                  {checked && <Check className="size-3" />}
                </span>
                <span className="min-w-0 flex-1">
                  <span className={`block text-xs font-medium ${highRisk ? 'text-destructive' : 'text-foreground'}`}>
                    {option.label}
                  </span>
                  <span className="mt-0.5 block text-[10.5px] leading-relaxed text-muted-foreground">
                    {option.description}
                  </span>
                </span>
              </button>
            );
          })}

          <p className="border-t border-border/70 px-2 pb-1 pt-1.5 text-[10px] leading-relaxed text-muted-foreground/80">
            仅保存在本机，按会话生效；未装配工具对应的权限不会生效。未授予的权限会在 Agent
            执行前弹审批；关闭页面不等于同意。
          </p>
        </div>
      )}
    </div>
  );
}
