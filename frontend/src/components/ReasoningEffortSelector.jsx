import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Brain, Check, ChevronDown } from 'lucide-react';

export function ReasoningEffortSelector({
  supportedLevels = [],
  currentEffort = 'medium',
  onSelectEffort,
  disabled = false,
}) {
  const [isOpen, setIsOpen] = useState(false);
  const containerRef = useRef(null);
  const triggerRef = useRef(null);

  // 备选等级列表：off 加上模型配置支持的具体参数值
  const effectiveLevels = React.useMemo(() => {
    let levels = [...supportedLevels];
    if (levels.length === 0) {
      levels = ['low', 'medium', 'high'];
    }
    if (!levels.includes('off') && !levels.includes('none')) {
      levels = ['off', ...levels];
    }
    return levels;
  }, [supportedLevels]);

  const close = useCallback((restoreFocus = false) => {
    setIsOpen(false);
    if (restoreFocus) triggerRef.current?.focus();
  }, []);

  const open = useCallback(() => {
    if (disabled) return;
    setIsOpen(true);
  }, [disabled]);

  // 点击外部 / Esc 关闭
  useEffect(() => {
    if (!isOpen) return undefined;

    const handlePointerDown = (event) => {
      if (containerRef.current && !containerRef.current.contains(event.target)) {
        close(false);
      }
    };
    const handleKeyDown = (event) => {
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

  const handleSelect = (level) => {
    onSelectEffort(level);
    close(true);
  };

  const isOff = currentEffort === 'off' || currentEffort === 'none';

  return (
    <div className="relative inline-block" ref={containerRef}>
      <button
        ref={triggerRef}
        type="button"
        disabled={disabled}
        onClick={() => (isOpen ? close(false) : open())}
        aria-haspopup="listbox"
        aria-expanded={isOpen}
        aria-label="reasoning_effort"
        title={`reasoning_effort: ${currentEffort}`}
        className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border transition cursor-pointer shadow-xs text-xs font-mono ${
          disabled
            ? 'opacity-50 cursor-not-allowed border-border/40 bg-muted/20 text-muted-foreground'
            : isOff
            ? 'border-border/60 bg-muted/30 hover:bg-muted text-muted-foreground hover:text-foreground'
            : 'border-primary/30 bg-primary/5 text-primary hover:bg-primary/10'
        }`}
      >
        <Brain
          size={13}
          className={`shrink-0 ${isOff || disabled ? 'text-muted-foreground' : 'text-primary'}`}
        />
        <span className="truncate text-xs">{currentEffort}</span>
        <ChevronDown
          size={12}
          className={`shrink-0 transition-transform ${isOpen ? 'rotate-180' : ''} ${
            isOff || disabled ? 'text-muted-foreground' : 'text-primary/70'
          }`}
        />
      </button>

      {isOpen && (
        <div className="absolute left-0 bottom-full mb-1.5 w-36 rounded-xl border border-border bg-popover text-popover-foreground shadow-xl p-1 z-50 text-xs backdrop-blur-md">
          <div className="px-2 py-1 text-[10px] font-mono text-muted-foreground border-b border-border/50 select-none">
            reasoning_effort
          </div>

          <div role="listbox" aria-label="reasoning_effort" className="p-0.5 space-y-0.5 mt-0.5">
            {effectiveLevels.map((lvl) => {
              const isSelected =
                currentEffort === lvl ||
                ((lvl === 'off' || lvl === 'none') && (currentEffort === 'off' || currentEffort === 'none'));

              return (
                <button
                  key={lvl}
                  type="button"
                  role="option"
                  aria-selected={isSelected}
                  onClick={() => handleSelect(lvl)}
                  className={`w-full text-left px-2 py-1.5 rounded-lg flex items-center justify-between font-mono text-xs transition cursor-pointer focus:outline-none focus-visible:ring-1 focus-visible:ring-ring/60 ${
                    isSelected
                      ? 'bg-muted text-foreground font-semibold'
                      : 'hover:bg-muted/60 text-muted-foreground hover:text-foreground'
                  }`}
                >
                  <span className="truncate">{lvl}</span>
                  {isSelected && <Check size={13} className="text-foreground shrink-0 ml-1" />}
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
