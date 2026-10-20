import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Brain, Check, ChevronDown } from 'lucide-react';

import { OFF, isOff, normalizeEffort } from '../lib/reasoning-effort';

export interface ReasoningEffortSelectorProps {
  /** 可选等级（含 OFF）。 */
  levels?: string[];
  currentEffort?: string;
  onSelectEffort: (effort: string) => void;
  disabled?: boolean;
}

/**
 * 思考等级选择器。
 *
 * R-7：可选等级由调用方用 `levelsFor(model)` 算好后传入。本组件不再自己兜底
 * `['low','medium','high']`——那会绕过后端「该模型不支持思考」的声明。
 *
 * 关闭标记（`off`/`none`/`false`）的判定统一走 `reasoning-effort.ts`，不再就地写
 * `currentEffort === 'off' || currentEffort === 'none'`。
 */
export function ReasoningEffortSelector({
  levels = [],
  currentEffort = 'medium',
  onSelectEffort,
  disabled = false,
}: ReasoningEffortSelectorProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const containerRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([]);

  const isOffNow = isOff(currentEffort);
  // normalizeEffort 可能返回 null（空串/非字符串）。null 在字符串数组里 indexOf
  // 必然得 -1，所以这里直接归为 -1，而不是把 null 泄给 indexOf。
  const normalizedEffort = normalizeEffort(currentEffort);
  const selectedIndex = normalizedEffort ? levels.indexOf(normalizedEffort) : -1;

  const close = useCallback((restoreFocus = false) => {
    setIsOpen(false);
    setActiveIndex(-1);
    if (restoreFocus) triggerRef.current?.focus();
  }, []);

  const open = useCallback(() => {
    if (disabled || levels.length === 0) return;
    setActiveIndex(selectedIndex >= 0 ? selectedIndex : 0);
    setIsOpen(true);
  }, [disabled, levels.length, selectedIndex]);

  // 点击外部 / Esc 关闭
  useEffect(() => {
    if (!isOpen) return undefined;

    const handlePointerDown = (event: PointerEvent) => {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) close(false);
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        close(true);
      }
    };

    document.addEventListener('pointerdown', handlePointerDown);
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [isOpen, close]);

  // 键盘导航时把焦点移到对应选项（与 ModelSelector 保持一致）
  useEffect(() => {
    if (!isOpen || activeIndex < 0) return;
    itemRefs.current[activeIndex]?.focus();
  }, [isOpen, activeIndex]);

  const handleListKeyDown = (event: React.KeyboardEvent) => {
    if (levels.length === 0) return;

    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setActiveIndex((index) => (index + 1) % levels.length);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      setActiveIndex((index) => (index - 1 + levels.length) % levels.length);
    } else if (event.key === 'Home') {
      event.preventDefault();
      setActiveIndex(0);
    } else if (event.key === 'End') {
      event.preventDefault();
      setActiveIndex(levels.length - 1);
    }
  };

  const handleSelect = (level: string) => {
    onSelectEffort(level);
    close(true);
  };

  // Tab 把焦点移出容器（含移出到浏览器 UI：relatedTarget 为 null）时关闭。只监听
  // pointerdown 与 Esc 会漏掉键盘用户：弹层留在原地挡住页面，还得再点一次才消失。
  const handleBlur = (event: React.FocusEvent<HTMLDivElement>) => {
    const next = event.relatedTarget;
    if (isOpen && !(next instanceof Node && containerRef.current?.contains(next))) close(false);
  };

  return (
    <div className="relative inline-block shrink-0" ref={containerRef} onBlur={handleBlur}>
      <button
        ref={triggerRef}
        type="button"
        disabled={disabled}
        onClick={() => (isOpen ? close(false) : open())}
        onKeyDown={(event) => {
          if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            open();
          }
        }}
        aria-haspopup="listbox"
        aria-expanded={isOpen}
        aria-label={`思考等级：${currentEffort}`}
        title={`reasoning_effort: ${currentEffort}`}
        className={`flex items-center gap-1 px-2 py-1.5 sm:gap-1.5 sm:px-2.5 rounded-lg border transition cursor-pointer shadow-xs text-xs font-mono pointer-coarse:min-h-9 ${
          disabled
            ? 'opacity-50 cursor-not-allowed border-border/40 bg-muted/20 text-muted-foreground'
            : isOffNow
              ? 'border-border/60 bg-muted/30 hover:bg-muted text-muted-foreground hover:text-foreground'
              : 'border-primary/30 bg-primary/5 text-primary hover:bg-primary/10'
        }`}
      >
        <Brain
          size={13}
          className={`hidden shrink-0 min-[480px]:block ${
            isOffNow || disabled ? 'text-muted-foreground' : 'text-primary'
          }`}
          aria-hidden="true"
        />
        {/*
          窄屏不再只留图标：只剩一个大脑图标时，用户不知道当前是 low / medium / high。
          360px 起显示文字（极窄的 320px 级屏幕仍然隐藏，优先保证不换行挤压发送按钮）。
          思考等级是三个短词，必须完整可读，因此它不参与压缩：工具条空间不够时先截断
          模型名（模型选择器有 min-w-0），不要把「low」缩成「l…」。480px 以下隐藏
          大脑图标，把宽度让给文字本身。
        */}
        <span className="hidden max-w-[3.5rem] truncate text-xs min-[360px]:inline">
          {currentEffort}
        </span>
        <ChevronDown
          size={12}
          className={`shrink-0 transition-transform ${isOpen ? 'rotate-180' : ''} ${
            isOffNow || disabled ? 'text-muted-foreground' : 'text-primary/70'
          }`}
          aria-hidden="true"
        />
      </button>

      {isOpen && (
        <div className="absolute left-0 bottom-full mb-1.5 w-36 rounded-xl border border-border bg-popover text-popover-foreground shadow-xl p-1 z-50 text-xs backdrop-blur-md">
          <div className="px-2 py-1 text-[10px] font-mono text-muted-foreground border-b border-border/50 select-none">
            reasoning_effort
          </div>

          <div
            role="listbox"
            aria-label="思考等级"
            onKeyDown={handleListKeyDown}
            className="p-0.5 space-y-0.5 mt-0.5"
          >
            {levels.map((level, index) => {
              const isSelected = normalizeEffort(currentEffort) === level;
              return (
                <button
                  key={level}
                  ref={(element) => {
                    itemRefs.current[index] = element;
                  }}
                  type="button"
                  role="option"
                  aria-selected={isSelected}
                  onFocus={() => setActiveIndex(index)}
                  onClick={() => handleSelect(level)}
                  className={`w-full text-left px-2 py-1.5 rounded-lg flex items-center justify-between font-mono text-xs transition cursor-pointer focus:outline-none focus-visible:ring-1 focus-visible:ring-ring/60 ${
                    isSelected
                      ? 'bg-muted text-foreground font-semibold'
                      : 'hover:bg-muted/60 text-muted-foreground hover:text-foreground'
                  }`}
                >
                  <span className="truncate">{level === OFF ? 'off（关闭）' : level}</span>
                  {isSelected && <Check size={13} className="text-foreground shrink-0 ml-1" aria-hidden="true" />}
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
