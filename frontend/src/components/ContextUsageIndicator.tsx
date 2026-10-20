import { type FC, useCallback, useEffect, useRef, useState } from 'react';
import { Gauge } from 'lucide-react';

import {
  describeContextUsage,
  formatPercent,
  formatTokens,
  type ContextUsageView,
} from '@/lib/context-usage';
import type { UsageState } from '@/types';

export interface ContextUsageIndicatorProps {
  /** 最近一轮的真实用量；没有（首轮 / 上游没报）时整个指示器不出现。 */
  usage: UsageState | null;
  /** 产生这份用量的模型 id（用来判断窗口是否已经因为切模型而失效）。 */
  usageModel?: string | null;
  /** 当前选中的模型 id。 */
  currentModel?: string | null;
  /** 当前选中模型的窗口（`/api/models`）。 */
  modelContextWindow?: number | null;
}

/**
 * 上下文用量指示器：**唯一**展示上下文用量的地方（输入框发送按钮左侧）。
 *
 * 为什么不在每条回答下面各摆一行：那是「那一轮请求」的快照，越往上越旧，
 * 看起来像「当前上下文」却答不上「现在还剩多少」。上下文是否会被锁住只跟
 * **最新**的那次请求有关，所以这里取整个会话里最后一次回报的用量。
 *
 * 悬浮/聚焦/点按都会展开详情（触屏没有 hover，点按是唯一入口）。
 */
export function ContextUsageIndicator({
  usage,
  usageModel = null,
  currentModel = null,
  modelContextWindow = null,
}: ContextUsageIndicatorProps) {
  const view = describeContextUsage(usage, { usageModel, currentModel, modelContextWindow });
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const closeTimer = useRef<number | null>(null);

  const cancelClose = useCallback(() => {
    if (closeTimer.current !== null) {
      window.clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
  }, []);

  // 指针移出后留一点时间给「从按钮挪进弹层」这段空隙；否则弹层会在半路消失
  const scheduleClose = useCallback(() => {
    cancelClose();
    closeTimer.current = window.setTimeout(() => {
      closeTimer.current = null;
      setOpen(false);
    }, 120);
  }, [cancelClose]);

  useEffect(() => cancelClose, [cancelClose]);

  // 触屏没有 hover：点开之后只能靠「点外面 / Esc」关掉。桌面端指针离开容器就会关，
  // 这里同时兜住「点开后又去点别处」的情况。
  useEffect(() => {
    if (!open) return undefined;
    const handlePointerDown = (event: PointerEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('pointerdown', handlePointerDown);
    document.addEventListener('keydown', handleKeyDown);
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown);
      document.removeEventListener('keydown', handleKeyDown);
    };
  }, [open]);

  if (!view) return null;

  const nearFull = view.usedPercent !== null && view.usedPercent >= 90;

  return (
    <div
      ref={containerRef}
      className="relative inline-flex"
      onPointerEnter={() => {
        cancelClose();
        setOpen(true);
      }}
      onPointerLeave={scheduleClose}
    >
      <button
        type="button"
        aria-label={`上下文用量：${view.summary}`}
        aria-haspopup="dialog"
        aria-expanded={open}
        // 不写原生 title：悬停时它会和自绘弹层同时出现，两块提示互相打架。
        // 完整数字在弹层与 aria-label 里（读屏走 aria-label）。
        onFocus={() => setOpen(true)}
        onBlur={scheduleClose}
        onClick={() => setOpen(true)}
        className={`flex h-7 items-center gap-1 rounded-lg px-1.5 text-[11px] transition-colors cursor-pointer focus-visible:ring-1 focus-visible:ring-ring/60 focus-visible:outline-none ${
          nearFull
            ? 'text-destructive hover:bg-destructive/10'
            : 'text-muted-foreground hover:bg-muted hover:text-foreground'
        }`}
      >
        <Gauge size={13} className="shrink-0" />
        {/* 窄屏只留图标：底部工具条本来就快挤满了，文字会把它顶出屏幕 */}
        <span className="hidden tabular-nums sm:inline">{view.compactLabel}</span>
      </button>

      {open && (
        <div
          role="dialog"
          aria-label="上下文用量详情"
          onPointerEnter={cancelClose}
          onPointerLeave={scheduleClose}
          className="absolute right-0 bottom-full z-50 mb-2 w-64 max-w-[calc(100vw-1.5rem)] rounded-xl border border-border bg-popover p-3 text-xs text-popover-foreground shadow-xl"
        >
          <div className="mb-2 flex items-baseline justify-between gap-2">
            <span className="text-xs font-medium text-foreground">上下文用量</span>
            <span className="text-[10px] text-muted-foreground">上一轮上游回报</span>
          </div>

          <UsageBar view={view} />

          <dl className="mt-2.5 space-y-1.5">
            <UsageRow
              label="已用"
              value={
                view.contextWindow === null
                  ? `${formatTokens(view.contextTokens)} tokens`
                  : `${formatTokens(view.contextTokens)} / ${formatTokens(view.contextWindow)}`
              }
              hint={view.usedPercent === null ? '窗口未知' : formatPercent(view.usedPercent)}
            />
            <UsageRow
              label="剩余"
              value={view.remainingTokens === null ? '—' : `${formatTokens(view.remainingTokens)} tokens`}
              hint={view.remainingPercent === null ? '窗口未知' : formatPercent(view.remainingPercent)}
            />
            <UsageRow
              label="缓存命中"
              value={
                view.cacheReadTokens > 0
                  ? `${formatTokens(view.cacheReadTokens)} tokens`
                  : '未命中'
              }
              hint={view.cachePercent === null ? '' : `缓存率 ${formatPercent(view.cachePercent)}`}
            />
            <UsageRow
              label="本轮输出"
              value={`${formatTokens(view.outputTokens)} tokens`}
              hint=""
            />
          </dl>

          <p className="mt-2.5 border-t border-border/60 pt-2 text-[10px] leading-relaxed text-muted-foreground">
            已用是上一轮请求的真实 prompt token（含系统提示词与整份历史），不含还没发送的草稿。
            {view.windowSource === 'reported' ? ' 窗口取自上游回报。' : null}
          </p>
        </div>
      )}
    </div>
  );
}

const UsageBar: FC<{ view: ContextUsageView }> = ({ view }) => {
  const percent = view.usedPercent ?? 0;
  const tone =
    percent >= 90 ? 'bg-destructive' : percent >= 70 ? 'bg-amber-500' : 'bg-primary';

  return (
    <div
      role="progressbar"
      aria-label="上下文占用"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Number(percent.toFixed(1))}
      className="h-1.5 w-full overflow-hidden rounded-full bg-muted"
    >
      <div
        className={`h-full rounded-full transition-[width] duration-300 ${tone}`}
        style={{ width: `${Math.min(Math.max(percent, 0), 100)}%` }}
      />
    </div>
  );
};

const UsageRow: FC<{ label: string; value: string; hint: string }> = ({
  label,
  value,
  hint,
}) => (
  <div className="flex items-baseline justify-between gap-2">
    <dt className="text-muted-foreground">{label}</dt>
    <dd className="flex items-baseline gap-1.5 tabular-nums">
      <span className="text-foreground">{value}</span>
      {hint ? <span className="text-[10px] text-muted-foreground">{hint}</span> : null}
    </dd>
  </div>
);
