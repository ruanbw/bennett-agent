import {
  type FC,
  type ReactNode,
  type RefObject,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { createPortal } from 'react-dom';
import { Database, Gauge } from 'lucide-react';

import { ContextUsageIndicator, type ContextBreakdown } from './ContextUsageIndicator';
import { readUsage } from '@/lib/context-usage';
import {
  cacheHitPercent,
  formatCacheHitPercent,
  formatCompactTokens,
  formatDuration,
  formatExactTokens,
  formatTokensPerSecond,
  sessionTokensPerSecond,
  summarizeSession,
  type SessionStats,
  type SessionTokenTotals,
  type StatsMessageLike,
} from '@/lib/session-stats';
import {
  readPerformanceUsage,
  performanceUsageFromStorageEvent,
  type PerformanceUsage,
} from '@/lib/performance-usage';
import { usePerformanceUsage } from '@/context/performance-usage-context';
import type { UsageState } from '@/types';

/** 面板与触发器之间的空隙（DSH 的 `PANEL_GAP`）。 */
const PANEL_GAP = 8;
/** 面板离视口边缘的最小留白（DSH 的 `PANEL_MARGIN`）。 */
const PANEL_MARGIN = 12;

/** 同一时刻只开一个面板：三个药丸的弹层互斥，谁开谁把别人关掉。 */
type PanelKey = 'activity' | 'usage' | 'context';

export interface ComposerStatsProps {
  /**
   * 整个会话的消息数组（assistant-ui 的 `thread.messages`，引用稳定）。
   *
   * 刻意只收这一个数据入口：轮次/步数/计时/token 账单/最近一轮用量都从这里
   * 聚合。让调用方分别传会逼它在本组件之外再做一遍全量扫描，而**遍历不能进
   * `useAuiState` 的 selector**——每次返回新数组会让快照永远不相等，组件无限重渲染
   * （`thread.aui.tsx` 里那条注释说的就是这件事）。
   */
  messages: readonly StatsMessageLike[];
  /** 当前选中的模型 id（判断上下文预算是否已因切模型失效）。 */
  currentModel?: string | null;
  /** 当前选中模型的窗口（`/api/models` 的 `context_window`）。 */
  modelContextWindow?: number | null;
  /** 本会话已因上下文溢出被锁住。 */
  locked?: boolean;
  /** 上下文分段；后端目前不提供，缺失时圆环面板里的条是单色整条。 */
  breakdown?: ContextBreakdown | null;
  /**
   * 「性能与用量」档位的**受控覆盖**（测试用）。
   *
   * 默认由组件自己从 localStorage 读，并监听 `storage` 事件做多标签同步——设置页
   * 由 Lead 接线，不应该再让调用方转一手这个偏好。
   */
  performanceUsage?: PerformanceUsage;
  /** 初始展开哪个面板（测试用；浏览器里默认都收起）。 */
  initialOpen?: PanelKey | null;
  /** 上下文弹层初始是否展开（测试用）。 */
  initialContextOpen?: boolean;
}

interface DerivedStats {
  stats: SessionStats;
  tokens: SessionTokenTotals;
  /** 最近一轮的用量（上下文占比只跟它有关）。 */
  latestUsage: UsageState | null;
  /** 产生那份用量的模型 id。 */
  latestUsageModel: string | null;
}

/**
 * 从消息数组聚合出药丸需要的全部读数。
 *
 * 所有「拿不到」都保持 null / 0 而不是编造：药丸据此少渲染一项，绝不显示 0。
 */
function deriveStats(messages: readonly StatsMessageLike[]): DerivedStats {
  const { stats, tokens } = summarizeSession(messages);

  let latestUsage: UsageState | null = null;
  let latestUsageModel: string | null = null;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role !== 'assistant') continue;
    const custom = message.metadata?.custom;
    if (!custom) continue;
    const usage = readUsage(custom.usage);
    if (!usage) continue;
    latestUsage = usage;
    latestUsageModel = typeof custom.model === 'string' ? custom.model : null;
    break;
  }

  return { stats, tokens, latestUsage, latestUsageModel };
}

/**
 * 输入卡片下方的统计行：`1 轮 37 步 · 208 tok/s`、`1.7M tok · 缓存命中 96%`、`9% ⌾`。
 *
 * ## 三个药丸各自弹自己的面板
 *
 * 三个面板**互斥**：打开一个会把另外两个关掉。都是 fixed 弹层，不互斥的话（比如先点
 * activity 再点圆环）两块会叠在一起，后面那块正好盖住前面那块的读数。
 *
 * 面板 portal 到 body 并用 fixed 定位在触发器**上方**（gap 8px、离视口边缘 12px）。
 * 不用绝对定位挂在药丸内部：这一行在吸底输入框里，而输入框位于可滚动视口内，
 * 向上溢出的绝对定位面板会被视口的 `overflow-y-auto` 裁掉。
 *
 * ## 没有数据就整个药丸不渲染
 *
 * 显示 `0 tok/s` 会被读成「模型卡住了」，`缓存命中 0%` 会被读成「缓存一次都没生效」。
 * 两者都只是「这项没量到」。所以：
 *   - `steps === 0`（没有任何一轮带计时）→ 整个 activity 药丸不出现；
 *   - 没有计费输入也没有输出 → 整个 usage 药丸不出现；
 *   - 上下文圆环由 `ContextUsageIndicator` 自己判断（它还要处理锁定态）。
 *
 * ## 简洁档
 *
 * 「性能与用量 = 简洁」时 activity 只留 `208 tok/s`、usage 只留 `缓存命中 96%`，
 * 且都不弹面板。**上下文圆环不受影响**：它是「还能不能继续问」的唯一依据，
 * 属于会话状态而不是性能读数（DSH 里 ContextMeter 同样独立于 performanceUsage）。
 */
export const ComposerStats: FC<ComposerStatsProps> = ({
  messages,
  currentModel = null,
  modelContextWindow = null,
  locked = false,
  breakdown = null,
  performanceUsage,
  initialOpen = null,
  initialContextOpen = false,
}) => {
  const derived = useMemo(() => deriveStats(messages), [messages]);

  // 偏好：优先用 provider（设置页与这里共享同一份状态，改完立刻生效）。
  // 没有 provider 时（组件被单独渲染 / 测试）退回自己读 localStorage + storage 同步；
  // 再没有就是 `performanceUsage` 这个受控覆盖。
  //
  // 为什么必须优先 provider：`storage` 事件只在**其他**标签页写入时触发，同一个标签页里
  // 在设置页切换档位不会触发它——只靠 localStorage 的话，用户改完设置得刷新页面才生效。
  const fromContext = usePerformanceUsage();
  const [storedMode, setStoredMode] = useState<PerformanceUsage>(() => readPerformanceUsage());
  useEffect(() => {
    if (typeof window === 'undefined') return undefined;
    const onStorage = (event: StorageEvent) => {
      const next = performanceUsageFromStorageEvent(event);
      if (next !== null) setStoredMode(next);
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);
  const mode = performanceUsage ?? fromContext?.mode ?? storedMode;

  // `initialContextOpen` 直接折进同一个状态：三个面板共用「谁开着」这一个开关，
  // 否则初始化时圆环开着、统计面板也开着，两块 fixed 弹层会叠在一起。
  const [openPanel, setOpenPanel] = useState<PanelKey | null>(
    initialOpen ?? (initialContextOpen ? 'context' : null),
  );
  const toggle = useCallback(
    (key: PanelKey) => setOpenPanel((current) => (current === key ? null : key)),
    [],
  );
  // 传进 StatDialogPill 的 onToggle 必须是稳定引用：它在 useDismiss 的依赖里，
  // 每次渲染换一个新函数会让 Escape/pointerdown 监听反复解绑重绑。
  const toggleActivity = useCallback(() => toggle('activity'), [toggle]);
  const toggleUsage = useCallback(() => toggle('usage'), [toggle]);
  // 上下文圆环受控：它打开时要关掉另外两个（三个都是 fixed 弹层，叠在一起会互相遮挡）
  const contextOpen = openPanel === 'context';
  const setContextOpen = useCallback(
    (next: boolean) => setOpenPanel(next ? 'context' : null),
    [],
  );

  const speed = sessionTokensPerSecond(derived.stats);
  const speedText = speed === null ? null : formatTokensPerSecond(speed);
  const cacheHit = cacheHitPercent(derived.tokens);
  const cacheHitText = formatCacheHitPercent(cacheHit);
  const totalText =
    derived.tokens.totalTokens > 0
      ? `${formatCompactTokens(derived.tokens.totalTokens)} tok`
      : null;

  // 一段读数都没有时整行不渲染（连圆环也没有，由 ContextUsageIndicator 决定）
  const hasActivity = derived.stats.steps > 0;
  const hasUsage = derived.tokens.billedInputTokens > 0 || derived.tokens.outputTokens > 0;
  const hasContext = derived.latestUsage !== null || locked;

  // 整行空着时不占位：否则输入框下方会多一条 4px 的空行
  if (!hasActivity && !hasUsage && !hasContext) return null;

  return (
    <div
      data-slot="composer-stats"
      // 右对齐、与输入卡片留 4px：这一排是「输入框的附注」，不是独立的一行内容
      className="mt-1 flex flex-wrap items-center justify-end gap-x-1"
    >
      {hasActivity &&
        (mode === 'compact' ? (
          speedText !== null && (
            <PlainPill
              stat="activity"
              icon={<Gauge size={14} />}
              label={speedText}
            />
          )
        ) : (
          <ActivityPill
            stats={derived.stats}
            speedText={speedText}
            open={openPanel === 'activity'}
            onToggle={toggleActivity}
          />
        ))}

      {hasUsage &&
        (mode === 'compact' ? (
          cacheHitText !== null && (
            <PlainPill
              stat="usage"
              icon={<Database size={14} />}
              label={`缓存命中 ${cacheHitText}`}
            />
          )
        ) : (
          <UsagePill
            tokens={derived.tokens}
            totalText={totalText}
            cacheHit={cacheHit}
            cacheHitText={cacheHitText}
            open={openPanel === 'usage'}
            onToggle={toggleUsage}
          />
        ))}

      {/* 上下文圆环：不看 performanceUsage 档位，始终显示 */}
      <ContextUsageIndicator
        usage={derived.latestUsage}
        usageModel={derived.latestUsageModel}
        currentModel={currentModel}
        modelContextWindow={modelContextWindow}
        locked={locked}
        breakdown={breakdown}
        open={contextOpen}
        onOpenChange={setContextOpen}
      />
    </div>
  );
};

/** 药丸的公共外壳：字号 12px、行高 20px、上下 1px / 左右 8px、圆角 small。 */
const PILL_CLASS =
  'inline-flex max-w-full items-center gap-1.5 rounded-sm px-2 py-px text-xs leading-5 font-[inherit] tabular-nums whitespace-nowrap';

/** 不可点的读数（简洁档）。 */
const PlainPill: FC<{ stat: string; icon: ReactNode; label: ReactNode }> = ({
  stat,
  icon,
  label,
}) => (
  <span data-composer-stat={stat} className="inline-flex min-w-0">
    <span className={`${PILL_CLASS} text-muted-foreground`}>
      {icon}
      <span className="min-w-0 truncate">{label}</span>
    </span>
  </span>
);

/** 两项之间的 `·`：比正文更淡，不抢读数的注意力。 */
const Sep: FC = () => (
  <span className="mx-1.5 text-border" aria-hidden="true">
    ·
  </span>
);

/** 会话统计药丸：轮次 + 步数 + 输出速度，点开看用时明细。 */
const ActivityPill: FC<{
  stats: SessionStats;
  speedText: string | null;
  open: boolean;
  onToggle: () => void;
}> = ({ stats, speedText, open, onToggle }) => {
  const counts = `${stats.turns} 轮 ${stats.steps} 步`;
  const icon = <Gauge size={14} />;
  const label = (
    <>
      {counts}
      {speedText !== null && (
        <>
          <Sep />
          {speedText}
        </>
      )}
    </>
  );
  const hasRows = stats.llmMs > 0 || stats.toolMs > 0 || stats.ttftSteps > 0 || speedText !== null;
  if (!hasRows) {
    return <PlainPill stat="activity" icon={icon} label={label} />;
  }
  return (
    <StatDialogPill
      stat="activity"
      icon={icon}
      label={label}
      ariaLabel={speedText === null ? counts : `${counts} · ${speedText}`}
      title="会话统计"
      titleValue={undefined}
      open={open}
      onToggle={onToggle}
    >
      <dl data-session-stats-details className={DETAILS_CLASS}>
        {stats.llmMs > 0 && (
          <>
            <dt>模型用时</dt>
            <dd>{formatDuration(stats.llmMs)}</dd>
          </>
        )}
        {stats.toolMs > 0 && (
          <>
            <dt>工具调用用时</dt>
            <dd>{formatDuration(stats.toolMs)}</dd>
          </>
        )}
        {stats.ttftSteps > 0 && stats.ttftMs !== null && (
          <>
            <dt>首 token 平均（TTFT）</dt>
            <dd>{formatDuration(stats.ttftMs)}</dd>
          </>
        )}
        {speedText !== null && (
          <>
            <dt>输出速度（TPS）</dt>
            <dd>{speedText}</dd>
          </>
        )}
      </dl>
    </StatDialogPill>
  );
};

/** Token 用量药丸：总量 + 缓存命中，点开看精确分项。 */
const UsagePill: FC<{
  tokens: SessionTokenTotals;
  totalText: string | null;
  cacheHit: number | null;
  cacheHitText: string | null;
  open: boolean;
  onToggle: () => void;
}> = ({ tokens, totalText, cacheHit, cacheHitText, open, onToggle }) => {
  const icon = <Database size={14} />;
  const label = (
    <>
      {totalText}
      {totalText !== null && cacheHitText !== null && <Sep />}
      {cacheHitText !== null && `缓存命中 ${cacheHitText}`}
    </>
  );
  return (
    <StatDialogPill
      stat="usage"
      icon={icon}
      label={label}
      ariaLabel={
        cacheHitText === null ? (totalText ?? '') : `${totalText ?? ''} · 缓存命中 ${cacheHitText}`
      }
      title="Token 用量"
      // 右上角给精确总量：药丸上是 1.7M，这里回答「到底多少」
      titleValue={`${formatExactTokens(tokens.totalTokens)} tok`}
      open={open}
      onToggle={onToggle}
    >
      <dl data-session-stats-usage className={DETAILS_CLASS}>
        {cacheHit !== null && (
          <>
            <dt>缓存命中</dt>
            <dd>{cacheHitText}</dd>
          </>
        )}
        <dt>未缓存输入</dt>
        <dd>{formatExactTokens(tokens.uncachedInputTokens)}</dd>
        <dt>缓存读取</dt>
        <dd>{formatExactTokens(tokens.cacheReadTokens)}</dd>
        {/* 缓存写入：本项目上游不回报，值为 null（未知）。显示 0 会被读成「确实没写缓存」 */}
        {tokens.cacheWriteTokens !== null && tokens.cacheWriteTokens !== 0 && (
          <>
            <dt>缓存写入</dt>
            <dd>{formatExactTokens(tokens.cacheWriteTokens)}</dd>
          </>
        )}
        <dt>输出</dt>
        <dd>{formatExactTokens(tokens.outputTokens)}</dd>
      </dl>
    </StatDialogPill>
  );
};

/** stat-dialog 的 `<dl>` 网格：左标签右数值，数值右对齐 + tabular-nums。 */
const DETAILS_CLASS =
  'm-0 grid grid-cols-[minmax(76px,auto)_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-muted-foreground [&>dd]:m-0 [&>dd]:text-right [&>dd]:tabular-nums [&>dd]:text-secondary-foreground';

/**
 * 可点开的药丸：按钮 + portal 面板。
 *
 * 面板初始 `visibility: hidden` 但**已经布局**，这样位置计算能测到真实尺寸
 * （DSH 的 MEASURE_STYLE 是同一招）。SSR 没有 `document.body` 可 portal，
 * 退化成内联渲染——静态 HTML 里仍能看到面板内容，测试与首屏一致。
 */
const StatDialogPill: FC<{
  stat: string;
  icon: ReactNode;
  label: ReactNode;
  ariaLabel: string;
  title: string;
  titleValue: string | undefined;
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}> = ({ stat, icon, label, ariaLabel, title, titleValue, open, onToggle, children }) => {
  const anchorRef = useRef<HTMLSpanElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const { pos, attachPanel } = useAnchoredPosition(open, anchorRef, panelRef);

  useDismiss(anchorRef, panelRef, open, onToggle);

  const panel = (
    <div
      ref={attachPanel}
      role="dialog"
      aria-label={title}
      style={pos ?? MEASURE_STYLE}
      className="fixed z-50 box-border w-max min-w-[min(300px,calc(100vw-1.5rem))] max-w-[min(440px,calc(100vw-1.5rem))] rounded-xl border border-border bg-popover p-4 text-xs leading-[18px] text-muted-foreground shadow-xl cursor-default"
    >
      <div className="mb-2 flex justify-between gap-4 font-medium text-foreground">
        <span className="inline-flex min-w-0 items-center gap-1.5">
          {icon}
          {title}
        </span>
        {titleValue !== undefined && <span className="tabular-nums">{titleValue}</span>}
      </div>
      <div className="mb-2.5 border-t border-border" aria-hidden="true" />
      {children}
    </div>
  );

  // 浏览器里 portal 到 body；SSR 没有 body，内联渲染（否则 static markup 里什么都看不到）
  const portaled =
    typeof document !== 'undefined' && document.body ? createPortal(panel, document.body) : panel;

  return (
    <span ref={anchorRef} data-composer-stat={stat} className="inline-flex min-w-0">
      <button
        type="button"
        className={`${PILL_CLASS} cursor-pointer text-muted-foreground hover:bg-muted hover:text-foreground aria-expanded:bg-muted aria-expanded:text-foreground focus-visible:ring-1 focus-visible:ring-ring/60 focus-visible:outline-none`}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={ariaLabel}
        onClick={onToggle}
      >
        {icon}
        <span className="min-w-0 truncate">{label}</span>
      </button>
      {open && portaled}
    </span>
  );
};

/** 位置未算出来时的面板样式：已布局但不可见。 */
const MEASURE_STYLE = { visibility: 'hidden' as const, left: 0, top: 0 };

/**
 * 把面板放在触发器上方：gap 8px，并夹在视口内（左右上下各留 12px）。
 *
 * `null` = 还没量到（首帧），调用方用 `MEASURE_STYLE` 渲染——先布局再定位，
 * 避免面板在错的位置闪一下。
 */
function useAnchoredPosition(
  open: boolean,
  anchorRef: RefObject<HTMLElement | null>,
  panelRef: RefObject<HTMLElement | null>,
): {
  pos: { left: number; top: number } | null;
  attachPanel: (node: HTMLDivElement | null) => void;
} {
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);

  /** 量的那一刻才知道真实尺寸，所以测量放在 panel 的 ref 回调里（挂载即测）。 */
  const measure = useCallback(() => {
    const anchor = anchorRef.current;
    const panel = panelRef.current;
    if (!anchor || !panel) {
      setPos(null);
      return;
    }
    const anchorRect = anchor.getBoundingClientRect();
    const panelRect = panel.getBoundingClientRect();
    const maxLeft = window.innerWidth - PANEL_MARGIN - panelRect.width;
    const left = Math.min(Math.max(anchorRect.right - panelRect.width, PANEL_MARGIN), maxLeft);
    const maxTop = window.innerHeight - PANEL_MARGIN - panelRect.height;
    const top = Math.min(Math.max(anchorRect.top - PANEL_GAP - panelRect.height, PANEL_MARGIN), maxTop);
    setPos({ left, top });
  }, [anchorRef, panelRef]);

  // 面板只在打开时挂载：它的 ref 回调就是「该量了」的信号。关闭时回调拿到 null，
  // 顺手把位置清掉，下次打开重新量——不留上一次的旧坐标。
  const attachPanel = useCallback(
    (node: HTMLDivElement | null) => {
      panelRef.current = node;
      measure();
    },
    [measure, panelRef],
  );

  useEffect(() => {
    if (!open) return undefined;
    // 输入框是吸底的：页面滚动时触发器位置会变，必须跟着重算
    const update = () => measure();
    window.addEventListener('resize', update);
    window.addEventListener('scroll', update, true);
    return () => {
      window.removeEventListener('resize', update);
      window.removeEventListener('scroll', update, true);
    };
  }, [open, measure]);

  return { pos, attachPanel };
}

/**
 * Escape / 外部 pointerdown / 外部 click 关闭。
 *
 * 三个都挂上：触屏只有 pointerdown，鼠标点击要覆盖「点在面板里的可聚焦元素」这种情况，
 * 键盘只有 Escape。
 */
function useDismiss(
  anchorRef: RefObject<HTMLElement | null>,
  panelRef: RefObject<HTMLElement | null>,
  open: boolean,
  onToggle: () => void,
): void {
  useEffect(() => {
    if (!open) return undefined;
    const isInside = (target: EventTarget | null): boolean =>
      target instanceof Node &&
      (anchorRef.current?.contains(target) === true || panelRef.current?.contains(target) === true);
    const onPointerDown = (event: PointerEvent) => {
      if (!isInside(event.target)) onToggle();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onToggle();
    };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open, onToggle, anchorRef, panelRef]);
}
