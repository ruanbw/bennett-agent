import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AlertCircle, Check, ChevronDown, Cpu, RefreshCw } from 'lucide-react';

export function ModelSelector({
  models = [],
  modelsError = null,
  currentModel = null,
  onSelectModel,
  onRefreshModels,
}) {
  const [isOpen, setIsOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const [isRetrying, setIsRetrying] = useState(false);
  const containerRef = useRef(null);
  const triggerRef = useRef(null);
  const itemRefs = useRef([]);

  const isEmpty = models.length === 0;

  const close = useCallback((restoreFocus = false) => {
    setIsOpen(false);
    setActiveIndex(-1);
    if (restoreFocus) triggerRef.current?.focus();
  }, []);

  const open = useCallback(() => {
    const index = models.findIndex((m) => m.id === currentModel);
    setActiveIndex(index >= 0 ? index : 0);
    setIsOpen(true);
  }, [models, currentModel]);

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

  // 键盘导航时把焦点移到对应选项
  useEffect(() => {
    if (!isOpen || activeIndex < 0) return;
    itemRefs.current[activeIndex]?.focus();
  }, [isOpen, activeIndex]);

  const handleListKeyDown = (event) => {
    if (isEmpty) return;

    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setActiveIndex((index) => (index + 1) % models.length);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      setActiveIndex((index) => (index - 1 + models.length) % models.length);
    } else if (event.key === 'Home') {
      event.preventDefault();
      setActiveIndex(0);
    } else if (event.key === 'End') {
      event.preventDefault();
      setActiveIndex(models.length - 1);
    }
  };

  const handleSelect = (model) => {
    onSelectModel(model.id);
    close(true);
  };

  const handleRetry = async () => {
    if (!onRefreshModels || isRetrying) return;
    setIsRetrying(true);
    try {
      await onRefreshModels();
    } finally {
      setIsRetrying(false);
    }
  };

  const selected = models.find((m) => m.id === currentModel);
  const label = selected?.name || currentModel || '选择模型';

  return (
    <div className="relative inline-block" ref={containerRef}>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => (isOpen ? close(false) : open())}
        onKeyDown={(event) => {
          if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            open();
          }
        }}
        aria-haspopup="listbox"
        aria-expanded={isOpen}
        aria-label="选择模型"
        className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg border border-border/60 bg-background hover:bg-muted text-xs font-medium text-foreground/80 hover:text-foreground transition cursor-pointer shadow-xs"
      >
        <Cpu size={13} className="text-muted-foreground shrink-0" />
        <span className="max-w-[130px] sm:max-w-[170px] truncate text-[11px] font-medium">
          {label}
        </span>
        <ChevronDown
          size={11}
          className={`text-muted-foreground shrink-0 transition-transform ${isOpen ? 'rotate-180' : ''}`}
        />
      </button>

      {isOpen && (
        <div className="absolute left-0 bottom-full mb-1.5 w-64 max-w-[calc(100vw-1.5rem)] rounded-xl border border-border bg-popover text-popover-foreground shadow-xl p-1 z-50 text-xs backdrop-blur-md">
          <div className="px-2.5 py-1 text-[10px] font-semibold text-muted-foreground border-b border-border/50 select-none flex justify-between items-center">
            <span>切换对话模型</span>
            <span>{isEmpty ? '不可用' : `${models.length} 款可用`}</span>
          </div>

          {isEmpty ? (
            <div className="flex flex-col items-center gap-2 px-3 py-4 text-center">
              <AlertCircle size={16} className="text-destructive/80" />
              <p className="text-[11px] text-muted-foreground leading-relaxed">
                {modelsError || '暂无可选模型'}
              </p>
              {onRefreshModels && (
                <button
                  type="button"
                  onClick={handleRetry}
                  disabled={isRetrying}
                  className="flex items-center gap-1.5 rounded-md border border-border/60 px-2.5 py-1 text-[11px] text-foreground/80 hover:bg-muted transition cursor-pointer disabled:opacity-60 disabled:cursor-default"
                >
                  <RefreshCw size={11} className={isRetrying ? 'animate-spin' : ''} />
                  {isRetrying ? '刷新中...' : '重试'}
                </button>
              )}
            </div>
          ) : (
            <div
              role="listbox"
              aria-label="可用模型"
              onKeyDown={handleListKeyDown}
              className="max-h-60 overflow-y-auto p-0.5 space-y-0.5 mt-0.5"
            >
              {models.map((model, index) => (
                <button
                  key={model.id}
                  ref={(element) => {
                    itemRefs.current[index] = element;
                  }}
                  type="button"
                  role="option"
                  aria-selected={currentModel === model.id}
                  onFocus={() => setActiveIndex(index)}
                  onClick={() => handleSelect(model)}
                  className={`w-full text-left px-2.5 py-1.5 rounded-lg flex items-center justify-between transition cursor-pointer focus:outline-none focus-visible:ring-1 focus-visible:ring-ring/60 ${
                    currentModel === model.id
                      ? 'bg-muted text-foreground font-medium'
                      : 'hover:bg-muted/60 text-muted-foreground hover:text-foreground'
                  }`}
                >
                  <div className="flex flex-col truncate pr-2">
                    <span className="truncate text-xs">{model.name}</span>
                    <span className="text-[10px] text-muted-foreground/80 truncate">
                      {model.provider}
                    </span>
                  </div>
                  <div className="flex items-center gap-1 shrink-0">
                    {model.reasoning && (
                      <span className="text-[9px] bg-muted px-1 py-0.2 rounded border border-border/50 text-muted-foreground">
                        思考
                      </span>
                    )}
                    {currentModel === model.id && (
                      <Check size={13} className="text-foreground ml-0.5" />
                    )}
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
