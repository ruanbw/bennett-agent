import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AlertCircle, Check, ChevronDown, Cpu, RefreshCw } from 'lucide-react';

import type { ModelItem } from '@/types';
import { formatTokens } from '@/lib/context-usage';
import { modelFullLabel, modelSubtitle } from '@/lib/model-identity';

export interface ModelSelectorProps {
  models?: ModelItem[];
  modelsError?: string | null;
  currentModel?: string | null;
  onSelectModel: (modelId: string) => void;
  /**
   * 重试刷新模型列表。
   *
   * 返回 Promise 时按钮会一直禁用（转圈）到它结束。父层（App.tsx 的
   * `refreshModels`）目前只 bump 一个 token 就返回 void，`await` 立刻通过、转圈只
   * 闪一帧——把「真正重新拉取」的 Promise 返回上来才给得出真实反馈。
   */
  onRefreshModels?: () => void | Promise<unknown>;
}

/**
 * 模型选择器。
 *
 * 模型列表与错误文案都由调用方持有：这里不自己拉取 `/api/models`，重试也只
 * 「请求一次刷新」（返回 Promise 时按钮进入转圈态），刷新成败的呈现交给上游。
 */
export function ModelSelector({
  models = [],
  modelsError = null,
  currentModel = null,
  onSelectModel,
  onRefreshModels,
}: ModelSelectorProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const [isRetrying, setIsRetrying] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const retryInFlight = useRef(false);

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

    // event.target 的类型是 EventTarget | null，而 contains 只接受 Node：document 上的
    // pointerdown 只会由已挂载的节点触发，断言成 Node 不会掩盖真实错误。
    const handlePointerDown = (event: PointerEvent) => {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        close(false);
      }
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

  // 键盘导航时把焦点移到对应选项
  useEffect(() => {
    if (!isOpen || activeIndex < 0) return;
    itemRefs.current[activeIndex]?.focus();
  }, [isOpen, activeIndex]);

  const handleListKeyDown = (event: React.KeyboardEvent) => {
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

  const handleSelect = (model: ModelItem) => {
    onSelectModel(model.id);
    close(true);
  };

  const handleRetry = async () => {
    // 用 ref 而不是 isRetrying 挡连点：setState 是异步的，同一帧里连点两下都会看到
    // isRetrying === false（父层返回 void 时尤其明显，整个重试只持续一帧）。
    if (!onRefreshModels || retryInFlight.current) return;
    retryInFlight.current = true;
    setIsRetrying(true);
    try {
      await onRefreshModels();
    } finally {
      retryInFlight.current = false;
      setIsRetrying(false);
    }
  };

  const selected = models.find((m) => m.id === currentModel);
  const label = selected?.name || (currentModel ? currentModel.split('/').pop() : '选择模型');

  // Tab 把焦点移出容器（含移出到浏览器 UI：relatedTarget 为 null）时关闭。只监听
  // pointerdown 与 Esc 会漏掉键盘用户：弹层留在原地挡住页面，还得再点一次才消失。
  const handleBlur = (event: React.FocusEvent<HTMLDivElement>) => {
    const next = event.relatedTarget;
    if (isOpen && !(next instanceof Node && containerRef.current?.contains(next))) close(false);
  };

  return (
    <div className="relative inline-block" ref={containerRef} onBlur={handleBlur}>
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
        /* 名字本身是「当前选了什么」的唯一可见证据，读屏也要能听到 */
        aria-label={`选择模型：${label}`}
        title={
          selected
            ? `${modelFullLabel(selected.name, selected)}${
                typeof selected.context_window === 'number' && selected.context_window > 0
                  ? ` · 上下文 ${formatTokens(selected.context_window)}`
                  : ''
              }`
            : undefined
        }
        className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border border-border/60 bg-muted/30 hover:bg-muted text-xs font-medium text-foreground/80 hover:text-foreground transition cursor-pointer shadow-xs"
      >
        <Cpu size={13} className="text-muted-foreground shrink-0" />
        {/*
          窄屏：64px 只够「DeepSee…」，既看不出型号也看不出版本。按可用宽度分档放宽
          （360px 起 7rem、380px 起 8rem，够放「DeepSeek V4.1 Flash」），
          桌面沿用 150px。320px 级极窄屏才退回 64px，避免把工具条挤到换行。
        */}
        <span className="max-w-[64px] min-[360px]:max-w-[7rem] min-[380px]:max-w-[8rem] sm:max-w-[150px] truncate text-xs font-medium">
          {label}
        </span>
        <ChevronDown
          size={12}
          className={`text-muted-foreground shrink-0 transition-transform ${isOpen ? 'rotate-180' : ''}`}
        />
      </button>

      {isOpen && (
        <div className="absolute left-0 bottom-full mb-1.5 w-72 max-w-[calc(100vw-1.5rem)] rounded-xl border border-border bg-popover text-popover-foreground shadow-xl p-1 z-50 text-xs backdrop-blur-md">
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
                    {/* 提供商会变化、模型 ID 才能区分同名的多条路由；截断由 CSS 负责，
                        完整值放 title 里供悬停查看 */}
                    <span
                      className="truncate text-[10px] text-muted-foreground/80"
                      title={modelSubtitle(model)}
                    >
                      {modelSubtitle(model)}
                    </span>
                  </div>
                  <div className="flex items-center gap-1 shrink-0">
                    {/* 上下文窗口是「还能不能继续问」的关键信息：上下文锁住的提示会让用户
                        换一个窗口更大的模型，这里直接把候选的窗口摆出来。网关没报时隐藏。 */}
                    {typeof model.context_window === 'number' && model.context_window > 0 && (
                      <span
                        className="text-[9px] tabular-nums bg-muted px-1 py-0.2 rounded border border-border/50 text-muted-foreground"
                        title={`上下文窗口：${model.context_window.toLocaleString('en-US')} tokens`}
                      >
                        {formatTokens(model.context_window)}
                      </span>
                    )}
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
