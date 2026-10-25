import { type FC, useCallback, useEffect, useRef, useState } from 'react';

import {
  CONTEXT_CRITICAL_PERCENT,
  CONTEXT_WARN_PERCENT,
  describeContextUsage,
  estimateRemainingChars,
  formatPercent,
  formatTokens,
  type ContextUsageView,
} from '@/lib/context-usage';
import { formatCompactTokens } from '@/lib/session-stats';
import type { UsageState } from '@/types';

/**
 * 圆环几何（与 DSH 的 ContextMeter 逐值对齐）：
 * 14×14 的 viewBox、半径 5.5、描边 2px。周长必须自己算——
 * `stroke-dasharray` 的单位是路径长度，不是百分比。
 */
const RADIUS = 5.5;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

/** 环上的百分比读数：整数，但「很小但不是零」写成 `<1%`（0% 会被读成没用量）。 */
function formatRingPercent(percent: number | null): string {
  if (percent === null) return '';
  if (percent > 0 && percent < 1) return '<1%';
  return `${Math.round(Math.min(Math.max(percent, 0), 100))}%`;
}

/**
 * 上下文面板里那条 4px 分段条的分段。
 *
 * 后端目前**没有**回报 `contextBreakdown`（系统提示词 / 工具 / 对话各占多少），
 * 所以调用方不传时这里只有一段单色整条。`null` = 拿不到分段，绝不按经验比例编造
 * 三段——那会让人以为「工具占了 30%」，而那个数字根本不存在。
 */
export interface ContextBreakdown {
  systemTokens: number;
  toolsTokens: number;
  messageTokens: number;
}

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
  /**
   * 上下文分段（系统提示词 / 工具 / 对话）。后端目前不提供，缺失时整条单色。
   * 传进来但三段都是 0 时同样退化成单色。
   */
  breakdown?: ContextBreakdown | null;
  /**
   * 弹层初始是否展开。默认 false。
   * 主要用于渲染测试（SSR 没有交互，收起的弹层根本不会出现在输出里）。
   */
  initialOpen?: boolean;
  /**
   * 受控的展开态（可选）。
   *
   * 只有同一排里还有别的弹出面板时才需要它——那时必须保证「同一时刻只开一个」，
   * 而本组件自己的 `open` 状态没法被同级兄弟面板关掉。不传就是自管理（默认）。
   */
  open?: boolean;
  /** 受控模式下的状态回传。 */
  onOpenChange?: (open: boolean) => void;
}

/**
 * 上下文用量指示器：**唯一**展示上下文用量的地方（输入卡片下方统计行里）。
 *
 * 视觉与 DSH 的 ContextMeter 一致：14px 圆环（2px 描边、从 12 点方向顺时针填充）
 * + 百分比读数。圆环比进度条更适合这个位置——它只占 14px，能在输入框下方那排小字
 * 里与其它读数并排，而横向进度条会吃掉整行宽度。
 *
 * 为什么不在每条回答下面各摆一行：那是「那一轮请求」的快照，越往上越旧，
 * 看起来像「当前上下文」却答不上「现在还剩多少」。上下文是否会被锁住只跟
 * **最新**的那次请求有关，所以这里取整个会话里最后一次回报的用量。
 *
 * 分母优先用后端随用量回报的「可用上限」（窗口扣掉回答预留与安全余量），而不是
 * 原始窗口：两者在 32k 模型上可能差一倍，用原始窗口会把「快满了」显示成「还很空」。
 *
 * 点按展开详情。**刻意不做 hover 展开**：它现在与另外两个统计药丸排在输入框下方
 * 同一行里，鼠标扫过整行会连续弹开三个面板，每个都盖住刚读到的数字。点按 + Escape /
 * 外部 pointerdown 关闭，与另外两个药丸一致；键盘用户 Tab 到别处也不会留下幽灵弹层。
 */
export function ContextUsageIndicator({
  usage,
  usageModel = null,
  currentModel = null,
  modelContextWindow = null,
  locked = false,
  breakdown = null,
  initialOpen = false,
  open: openProp,
  onOpenChange,
}: ContextUsageIndicatorProps) {
  const view = describeContextUsage(usage, { usageModel, currentModel, modelContextWindow });
  const [internalOpen, setInternalOpen] = useState(initialOpen);
  // 受控 / 自管理两态：不传 `open` 时行为与之前完全一致
  const controlled = openProp !== undefined;
  const open = controlled ? openProp : internalOpen;
  const setOpen = useCallback(
    (next: boolean) => {
      if (!controlled) setInternalOpen(next);
      onOpenChange?.(next);
    },
    [controlled, onOpenChange],
  );
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
  }, [cancelClose, setOpen]);

  useEffect(() => cancelClose, [cancelClose]);

  // 点开之后靠「点外面 / Esc」关掉（同一排里兄弟面板的打开也会把这里关掉——见
  // ComposerStats 的受控用法）。
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
  }, [open, setOpen]);

  // 锁定是持续状态，即使没有用量记录也必须能看到「为什么发不出去」。
  if (!view && !locked) return null;

  const usedPercent = view?.usedPercent ?? null;
  const nearFull = usedPercent !== null && usedPercent >= CONTEXT_CRITICAL_PERCENT;
  // 进度条在 70% 就变琥珀色：不给一句话解释，用户只会看到「变黄了」而不知道这意味着什么
  const warning = !nearFull && usedPercent !== null && usedPercent >= CONTEXT_WARN_PERCENT;
  const alerting = locked || nearFull;

  // 锁定态的环画满：真实占用比上一轮回报的更大（历史 + 回答预留），画成 60% 会自相矛盾
  const ringPercent = locked ? 100 : (usedPercent ?? 0);
  const reading = locked ? '已满' : (formatRingPercent(usedPercent) || view?.compactLabel || '');
  const ringLabel = locked ? '上下文已满，查看说明' : `上下文用量：${view?.summary ?? ''}`;

  return (
    <div
      ref={containerRef}
      className="relative inline-flex"
      // 容器只负责「指针进到弹层里别关」，**不**用 hover 打开面板。
      //
      // 上一版是 hover 打开；搬到输入框下方那排药丸之后必须改成点按：三个药丸紧挨着，
      // 鼠标从左往右扫一遍会连续弹三个面板，每次都盖住刚读到的数字。
      // 点按 + Escape/外部 pointerdown 关闭也与另外两个药丸一致。
      onPointerEnter={cancelClose}
      onPointerLeave={scheduleClose}
      onBlur={(event) => {
        // 焦点还在容器里（例如从按钮 Tab 到弹层里的可聚焦元素）就别关；
        // relatedTarget 为空表示焦点去了浏览器 UI / 页面外，应当关闭。
        if (!containerRef.current?.contains(event.relatedTarget as Node | null)) scheduleClose();
      }}
    >
      <button
        type="button"
        aria-label={ringLabel}
        aria-haspopup="dialog"
        aria-expanded={open}
        // 不写原生 title：悬停时它会和自绘弹层同时出现，两块提示互相打架。
        // 完整数字在弹层与 aria-label 里（读屏走 aria-label）。
        onClick={() => setOpen(!open)}
        className={`inline-flex h-5 items-center gap-1.5 rounded-sm px-2 text-xs leading-5 tabular-nums transition-colors cursor-pointer focus-visible:ring-1 focus-visible:ring-ring/60 focus-visible:outline-none ${
          alerting
            ? 'text-destructive hover:bg-destructive/10'
            : 'text-muted-foreground hover:bg-muted hover:text-foreground'
        }`}
      >
        <ContextRing percent={ringPercent} />
        <span className="whitespace-nowrap">{reading}</span>
      </button>

      {open && (
        <div
          role="dialog"
          aria-label="上下文用量详情"
          onPointerEnter={cancelClose}
          onPointerLeave={scheduleClose}
          /* 右对齐到圆环，而它离屏幕右缘只有一点内边距；320px 屏幕上按实际留白收窄，
             大屏仍由 264px（DSH 的 `min(264px, 100vw - 24px)`）限制。 */
          className="absolute right-0 bottom-full z-50 mb-2 w-[264px] max-w-[calc(100vw-1.5rem)] rounded-xl border border-border bg-popover p-3 text-xs text-popover-foreground shadow-xl"
        >
          <div className="mb-2 flex items-center gap-1.5">
            <span className="text-muted-foreground">{locked ? '上下文已满' : '上下文已用'}</span>
            {!locked && (
              <span className="font-medium text-foreground">{formatRingPercent(usedPercent)}</span>
            )}
            <span className="ml-auto font-medium tabular-nums text-foreground">
              {locked
                ? '本会话无法继续提问'
                : `~${formatCompactTokens(view?.contextTokens ?? 0)} / ${
                    view?.limitTokens === null || view?.limitTokens === undefined
                      ? '—'
                      : formatCompactTokens(view.limitTokens)
                  }`}
            </span>
          </div>

          {locked ? (
            <p className="rounded-lg border border-destructive/40 bg-destructive/10 px-2 py-1.5 text-[10px] leading-relaxed text-destructive">
              历史加上回复所需的空间已经超过模型上限，本轮请求被服务端拒绝。请新建会话，或换用上下文窗口更大的模型后继续。
            </p>
          ) : (
            view && (
              <>
                <UsageBar view={view} breakdown={breakdown} />
                {breakdown !== null && breakdownTotal(breakdown) > 0 && (
                  <dl className="mt-2 space-y-1">
                    <LegendRow
                      label="系统提示词"
                      tokens={breakdown.systemTokens}
                      dot="bg-[#94a3b8]"
                    />
                    <LegendRow label="工具定义" tokens={breakdown.toolsTokens} dot="bg-[#a78bfa]" />
                    <LegendRow label="对话消息" tokens={breakdown.messageTokens} dot="bg-[#3b82f6]" />
                  </dl>
                )}
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

                {estimateRemainingChars(view.remainingTokens) && (
                  <p className="mt-2 text-[10px] leading-relaxed text-muted-foreground">
                    {`还能写${estimateRemainingChars(view.remainingTokens)}（按中文 ≈1 字/token 粗估；英文约 4 字符/token，同样的余量能写的英文单词少得多）。`}
                  </p>
                )}
                {nearFull && (
                  <p className="mt-2 rounded-lg border border-destructive/40 bg-destructive/10 px-2 py-1.5 text-[10px] leading-relaxed text-destructive">
                    已接近可用上限，下一条长消息可能被拒绝。建议新建会话，或换用窗口更大的模型。
                  </p>
                )}
                {warning && (
                  <p className="mt-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-2 py-1.5 text-[10px] leading-relaxed text-amber-700 dark:text-amber-400">
                    已用过七成：还能继续聊，但长对话建议尽快新建会话，或换个窗口更大的模型——历史只会越来越长。
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
                {breakdown === null && (
                  <p className="mt-1.5 text-[10px] leading-relaxed text-muted-foreground/80">
                    这条进度只有总量：服务端还没有回报上下文各成分的占比，所以不拆分——编一个比例会比不显示更糟。
                  </p>
                )}
              </>
            )
          )}
        </div>
      )}
    </div>
  );
}

/** 14px 圆环：底环 + 从 12 点方向顺时针填充的进度环。 */
const ContextRing: FC<{ percent: number }> = ({ percent }) => {
  const clamped = Math.min(Math.max(percent, 0), 100);
  return (
    <svg viewBox="0 0 14 14" width="14" height="14" aria-hidden="true" className="shrink-0">
      <circle cx="7" cy="7" r={RADIUS} fill="none" strokeWidth={2} className="stroke-border" />
      <circle
        cx="7"
        cy="7"
        r={RADIUS}
        fill="none"
        strokeWidth={2}
        strokeLinecap="round"
        strokeDasharray={`${(CIRCUMFERENCE * clamped) / 100} ${CIRCUMFERENCE}`}
        transform="rotate(-90 7 7)"
        className="stroke-current transition-[stroke-dasharray] duration-300"
      />
    </svg>
  );
};

const breakdownTotal = (breakdown: ContextBreakdown): number =>
  breakdown.systemTokens + breakdown.toolsTokens + breakdown.messageTokens;

/**
 * 4px 分段条。
 *
 * 拿不到分段（后端不回报）时只有一段单色、宽度 = 已用占比——这正是 DSH 在
 * `contextBreakdown` 缺席时的做法。**不按经验比例编造三段**：那条条的每一段都在暗示
 * 「这一类占了多少」，编出来的比例会被当成事实。
 */
const UsageBar: FC<{ view: ContextUsageView; breakdown: ContextBreakdown | null }> = ({
  view,
  breakdown,
}) => {
  const percent = Math.min(Math.max(view.usedPercent ?? 0, 0), 100);
  // 阈值与「已接近可用上限」文案共用同一份常量：红条与警告必须同时出现。
  // 拿不到上限时（usedPercent 为 null）条是空的，tone 无所谓，用中性色。
  const tone =
    view.usedPercent === null
      ? 'bg-muted-foreground'
      : view.usedPercent >= CONTEXT_CRITICAL_PERCENT
        ? 'bg-destructive'
        : view.usedPercent >= CONTEXT_WARN_PERCENT
          ? 'bg-amber-500'
          : 'bg-primary';

  const total = breakdown === null ? 0 : breakdownTotal(breakdown);
  const segments =
    breakdown === null || total === 0
      ? [{ key: 'total', width: percent, dot: tone }]
      : [
          { key: 'system', width: (percent * breakdown.systemTokens) / total, dot: 'bg-[#94a3b8]' },
          { key: 'tools', width: (percent * breakdown.toolsTokens) / total, dot: 'bg-[#a78bfa]' },
          { key: 'messages', width: (percent * breakdown.messageTokens) / total, dot: 'bg-[#3b82f6]' },
        ].filter((segment) => segment.width > 0);

  return (
    <div
      role="progressbar"
      aria-label="上下文占用"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Number(percent.toFixed(1))}
      className="flex h-1 w-full gap-px overflow-hidden rounded-full bg-muted"
    >
      {segments.map((segment) => (
        <div
          key={segment.key}
          className={`h-full flex-none rounded-[1px] transition-[width] duration-300 ${segment.dot}`}
          style={{ width: `${Math.max(segment.width, 0)}%` }}
        />
      ))}
    </div>
  );
};

const LegendRow: FC<{ label: string; tokens: number; dot: string }> = ({
  label,
  tokens,
  dot,
}) => (
  <div className="flex items-center justify-between gap-3 py-0.5">
    <dt className="flex items-center gap-1.5 text-muted-foreground">
      <span className={`inline-block size-2 rounded-sm ${dot}`} aria-hidden="true" />
      {label}
    </dt>
    <dd className="m-0 tabular-nums text-foreground">{`~${formatCompactTokens(tokens)}`}</dd>
  </div>
);

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
