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
  /** 最近一轮的真实用量；没有（首轮 / 上游没报）且未锁定时整个指示器不出现。 */
  usage: UsageState | null;
  /** 产生这份用量的模型 id（用来判断窗口/预算是否已经因为切模型而失效）。 */
  usageModel?: string | null;
  /** 当前选中的模型 id。 */
  currentModel?: string | null;
  /** 当前选中模型的窗口（`/api/models`）。 */
  modelContextWindow?: number | null;
  /**
   * 本会话已因上下文溢出被锁住（composer 的 `contextLocked`）。
   *
   * 锁住时**不再展示上一轮的数字**：那是失败前最后一次成功请求的用量，而真正被拒的
   * 是「历史 + 回答预留」这个更大的量。显示旧数字（可能才 60%）会与「已满」自相矛盾。
   */
  locked?: boolean;
}

/**
 * 上下文用量指示器：**唯一**展示上下文用量的地方（输入框发送按钮左侧）。
 *
 * 为什么不在每条回答下面各摆一行：那是「那一轮请求」的快照，越往上越旧，
 * 看起来像「当前上下文」却答不上「现在还剩多少」。上下文是否会被锁住只跟
 * **最新**的那次请求有关，所以这里取整个会话里最后一次回报的用量。
 *
 * 分母优先用后端随用量回报的「可用上限」（窗口扣掉回答预留与安全余量），而不是
 * 原始窗口：两者在 32k 模型上可能差一倍，用原始窗口会把「快满了」显示成「还很空」。
 *
 * 悬浮/聚焦/点按都会展开详情（触屏没有 hover，点按是唯一入口）。指针移开只在
 * 焦点也离开容器后才关闭，键盘用户 Tab 到别处不会留下一个幽灵弹层。
 */
export function ContextUsageIndicator({
  usage,
  usageModel = null,
  currentModel = null,
  modelContextWindow = null,
  locked = false,
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

  // 锁定是持续状态，即使没有用量记录也必须能看到「为什么发不出去」。
  if (!view && !locked) return null;

  const usedPercent = view?.usedPercent ?? null;
  const nearFull = usedPercent !== null && usedPercent >= 90;
  const alerting = locked || nearFull;

  return (
    <div
      ref={containerRef}
      className="relative inline-flex"
      onPointerEnter={() => {
        cancelClose();
        setOpen(true);
      }}
      onPointerLeave={scheduleClose}
      onBlur={(event) => {
        // 焦点还在容器里（例如从按钮 Tab 到弹层里的可聚焦元素）就别关；
        // relatedTarget 为空表示焦点去了浏览器 UI / 页面外，应当关闭。
        if (!containerRef.current?.contains(event.relatedTarget as Node | null)) scheduleClose();
      }}
    >
      <button
        type="button"
        aria-label={locked ? '上下文已满，查看说明' : `上下文用量：${view?.summary ?? ''}`}
        aria-haspopup="dialog"
        aria-expanded={open}
        // 不写原生 title：悬停时它会和自绘弹层同时出现，两块提示互相打架。
        // 完整数字在弹层与 aria-label 里（读屏走 aria-label）。
        onFocus={() => setOpen(true)}
        onClick={() => setOpen(true)}
        className={`flex h-7 items-center gap-1 rounded-lg px-1.5 text-[11px] transition-colors cursor-pointer focus-visible:ring-1 focus-visible:ring-ring/60 focus-visible:outline-none ${
          alerting
            ? 'text-destructive hover:bg-destructive/10'
            : 'text-muted-foreground hover:bg-muted hover:text-foreground'
        }`}
      >
        <Gauge size={13} className="shrink-0" />
        {/* 窄屏只留图标：底部工具条本来就快挤满了，文字会把它顶出屏幕。
            异常态补一个红点，否则手机上只能看到一个同样大小的图标。 */}
        <span className="hidden tabular-nums sm:inline">{locked ? '已满' : view?.compactLabel}</span>
        {alerting && <span className="size-1.5 rounded-full bg-destructive sm:hidden" aria-hidden="true" />}
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
            <span className="text-xs font-medium text-foreground">{locked ? '上下文已满' : '上下文用量'}</span>
            <span className="text-[10px] text-muted-foreground">
              {locked ? '本会话无法继续提问' : '上一轮上游回报'}
            </span>
          </div>

          {locked ? (
            <p className="rounded-lg border border-destructive/40 bg-destructive/10 px-2 py-1.5 text-[10px] leading-relaxed text-destructive">
              历史加上回复所需的空间已经超过模型上限，本轮请求被服务端拒绝。请新建会话，或换用上下文窗口更大的模型后继续。
            </p>
          ) : (
            view && (
              <>
                <UsageBar view={view} />
                <dl className="mt-2.5 space-y-1.5">
                  <UsageRow
                    label="已用"
                    value={`${formatTokens(view.contextTokens)} tokens`}
                    hint={view.usedPercent === null ? '上限未知' : formatPercent(view.usedPercent)}
                  />
                  <UsageRow
                    label="剩余"
                    value={
                      view.remainingTokens === null ? '—' : `${formatTokens(view.remainingTokens)} tokens`
                    }
                    hint={view.remainingPercent === null ? '上限未知' : formatPercent(view.remainingPercent)}
                  />
                  {view.limitSource === 'budget' && view.limitTokens !== null ? (
                    <UsageRow
                      label="可用上限"
                      value={`${formatTokens(view.limitTokens)} tokens`}
                      hint={view.contextWindow === null ? '' : `窗口 ${formatTokens(view.contextWindow)}`}
                    />
                  ) : view.limitSource === 'window' && view.limitTokens !== null ? (
                    <UsageRow
                      label="模型窗口"
                      value={`${formatTokens(view.limitTokens)} tokens`}
                      hint="未含预算上限"
                    />
                  ) : null}
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

                {nearFull && (
                  <p className="mt-2 rounded-lg border border-destructive/40 bg-destructive/10 px-2 py-1.5 text-[10px] leading-relaxed text-destructive">
                    已接近可用上限，下一条长消息可能被拒绝。建议新建会话，或换用窗口更大的模型。
                  </p>
                )}

                <p className="mt-2.5 border-t border-border/60 pt-2 text-[10px] leading-relaxed text-muted-foreground">
                  已用是上一轮请求的真实 prompt token（含系统提示词与整份历史），不含还没发送的草稿。
                  {view.limitSource === 'budget'
                    ? ' 可用上限已扣除回答预留与安全余量，后端每轮按同一口径判定。'
                    : view.windowSource === 'reported'
                      ? ' 窗口取自上游回报。'
                      : view.windowSource === 'model'
                        ? ' 窗口取自当前模型配置。'
                        : null}
                </p>
              </>
            )
          )}
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
