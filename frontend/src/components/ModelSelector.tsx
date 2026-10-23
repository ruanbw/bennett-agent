import React, { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { AlertCircle, Check, ChevronDown, ChevronRight, Loader2, RefreshCw, X } from 'lucide-react';

import type { ModelItem } from '@/types';
import { levelsFor, normalizeEffort, shouldShowEffortSelector } from '@/lib/reasoning-effort';
import { modelFullLabel, modelSubtitle } from '@/lib/model-identity';
import { readModelSelection } from '@/lib/model-selection';
import {
  buildRootRows,
  cycleIndex,
  emptyCatalogText,
  effortLabel as formatEffort,
  filterModelGroups,
  groupModelsByProvider,
  orderProviderGroups,
  shouldShowModelSearch,
  triggerLabel as composeTriggerLabel,
} from '@/lib/model-menu';

export interface ModelSelectorProps {
  models?: ModelItem[];
  modelsError?: string | null;
  currentModel?: string | null;
  /** 当前思考等级（含 `'off'`）。模型不支持思考时这个值不参与渲染。 */
  currentReasoningEffort?: string;
  /**
   * 选择模型。
   *
   * 返回 Promise 时菜单保持打开到它结算：期间触发器把 chevron 换成加载图标、命中行把
   * ✓ 换成加载图标；被拒绝时菜单不关闭，把原因写在面板顶部。App 的 `selectModel` 正是
   * 异步的（要落 Dexie），所以这条路径是常态而不是边角。
   */
  onSelectModel: (modelId: string) => void | Promise<unknown>;
  /** 选择思考等级。返回 Promise 时的行为同上。 */
  onSelectEffort?: (level: string) => void | Promise<unknown>;
  /**
   * 重试刷新模型列表。
   *
   * 返回 Promise 时按钮一直禁用（转圈）到它结束：父层（App.tsx 的 `refreshModels`）现在
   * 返回「本次拉取真正结束」的 Promise，转圈才有真实含义。
   */
  onRefreshModels?: () => void | Promise<unknown>;
  /**
   * 目录是否正在加载。
   *
   * props 上没有独立的 loading 字段：「空数组 + 没有错误」既可能是首次加载中，也可能是
   * 真的没有模型。父层知道答案就传；不传时退化成「曾经拿到过列表就不再算加载中」。
   */
  modelsLoading?: boolean;
}

/** 根面板 / 下钻面板。 */
type Pane = 'root' | 'model' | 'effort';

/**
 * 已提交、还在等确认的选择。
 *
 * 只用于两处视觉反馈（触发器拿掉 chevron、命中行拿掉 ✓，都换成加载图标），同时是
 * 「正在提交」的闸门：期间不接受第二次选择，否则连点两下会把两次写入排成队。
 */
type PendingSelection =
  | { kind: 'model'; modelId: string }
  | { kind: 'effort'; effort: string };

/** 从下钻面板退回根面板时，把焦点交还给打开它的那一行。 */
type BackTarget = 'model' | 'effort';

function isThenable(value: unknown): value is PromiseLike<unknown> {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as { then?: unknown }).then === 'function'
  );
}

/** 失败文案：优先原消息，绝不把 `[object Object]` 摆到界面上。 */
function describeFailure(error: unknown): string {
  if (error instanceof Error && error.message) return error.message;
  if (typeof error === 'string' && error) return error;
  return '未知错误';
}

/**
 * 模型选择器（输入框左下角那一个控件）。
 *
 * 形态对齐 DSH 的 `ModelSelect`：**一个**触发器 + **两级**菜单。此前本项目是两个并排
 * 按钮（模型 / 思考等级），还在行内堆了提供商副标题、上下文徽标、「默认」「思考」小
 * 标签，窄屏上把发送按钮挤到第二行。收成一个触发器后：触发器是 `模型名 · 思考等级`
 * （等级用更淡的 caption 色），点开先出根面板两行（模型 / 思考等级），各自下钻。
 *
 * ## 保留的本项目逻辑（DSH 没有，但这里必须有）
 *
 * 1. `/api/models` 失败或返回空：错误文案 + 重试按钮（`onRefreshModels` 返回 Promise 时
 *    按钮转圈）。DSH 的目录由 host 持有，不存在「列表根本拉不下来」这个状态。
 * 2. 当前模型已不在可用列表（`selectedMissing`）：琥珀色告警 + 「切到默认模型」一键
 *    切换。管理员停用模型后，这是用户唯一能自救的入口。
 * 3. 思考等级来自 `levelsFor(model)`（含 `'off'`）；`shouldShowEffortSelector(model)` 为
 *    false 时不渲染等级行，点击直接进模型列表——不摆一个只有一行的空壳根面板。
 *
 * ## 为什么菜单是绝对定位而不是 portal
 *
 * 菜单贴在工具条上方，触发器就是它的锚点。portal + 手动测量要处理滚动、窗口缩放、
 * 菜单自身尺寸变化三种重排；而 composer 卡片没有 `overflow: hidden`，不会被裁掉。
 * 少一个会坏的环节。
 */
export function ModelSelector({
  models = [],
  modelsError = null,
  currentModel = null,
  currentReasoningEffort = '',
  onSelectModel,
  onSelectEffort = () => {},
  onRefreshModels,
  modelsLoading,
}: ModelSelectorProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [pane, setPane] = useState<Pane>('root');
  const [query, setQuery] = useState('');
  const [highlighted, setHighlighted] = useState<number | null>(null);
  /**
   * 选定后焦点回到触发器，但不该显示焦点环：那圈环在这里的含义是「菜单开着、键盘停在
   * 触发器上」，而此刻菜单已经关了。触发器失焦或菜单重开时清掉。
   */
  const [selectionFocus, setSelectionFocus] = useState(false);
  const [pending, setPending] = useState<PendingSelection | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [isRetrying, setIsRetrying] = useState(false);

  const containerRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const retryInFlight = useRef(false);
  /**
   * 下一次渲染后把焦点放到哪里。
   *
   * - `'drill'`：刚下钻（或根本没有根面板、直接打开模型列表）→ 去搜索框或当前项；
   * - `BackTarget`：从下钻面板退回 → 回到打开它的那一行；
   * - `null`：新打开的根面板 → **焦点留在触发器上**（DSH 的行为：先按 ↑/↓ 才进菜单）。
   */
  const focusIntent = useRef<'drill' | BackTarget | null>(null);
  const id = useId();

  /**
   * 「列表至少成功回来过一次」。
   *
   * props 上「空数组 + 无错误」既可能是首次加载中，也可能是真的没有模型。父层知道答案
   * 就传 `modelsLoading`；不传时用这个标记：一旦见过非空列表或错误，之后的空数组就不再
   * 被当成加载中（后端至少有兜底模型，真·空目录是异常态而不是加载态）。
   *
   * 用「渲染期调整 state」而不是 ref 或 effect：effect 晚一帧，会把「加载失败」先闪成
   * 一次「加载中…」；渲染期赋值是本组件自己的状态调整，React 允许且会立刻重渲染。
   */
  const [hasLoadedOnce, setHasLoadedOnce] = useState(
    () => models.length > 0 || modelsError !== null,
  );
  if (!hasLoadedOnce && (models.length > 0 || modelsError !== null)) setHasLoadedOnce(true);

  const { selected, selectedMissing, defaultModel } = readModelSelection(models, currentModel);

  const isEmpty = models.length === 0;
  const isLoading = modelsLoading ?? (isEmpty && modelsError === null && !hasLoadedOnce);

  /**
   * 是否有「思考等级」这一行：决定根面板有没有第二行、触发器有没有等级文字、Esc 与
   * Shift+Tab 有没有回退目标。
   *
   * 模型明确声明不支持思考（`reasoning === false` → `levelsFor` 返回空）时没有这一行，
   * 点击直接进模型列表——不摆一个只有一行的空壳根面板让用户多点一次。
   *
   * `levels.length > 0` 与 `shouldShowEffortSelector` 一起判断是不冗余的：前者是「实际
   * 有可选项」，后者是「模型没有明确否认思考」，少了任一条都可能渲染出一个空面板。
   */
  const levels = useMemo(() => levelsFor(selected), [selected]);
  const showEffort =
    Boolean(selected) && shouldShowEffortSelector(selected) && levels.length > 0;
  /**
   * 当前等级，已归一化成 `levelsFor` 的词表（`none`/`false` → `off`）。
   *
   * 不直接比较原始值：父层可能存着 `none`，而 `levelsFor` 给的是 `off`，逐字比较会让
   * 「关闭」这一项永远不显示 ✓。归一化只在本组件内做，不改父层状态。
   */
  const activeEffort = normalizeEffort(currentReasoningEffort) ?? '';
  /**
   * 触发器与根面板上的等级文字。
   *
   * 父层没传等级（空串）时显示「未选择」而不是空串：空串会让触发器留下一个悬空的
   * 分隔符（`模型名 · `），而这里确实是「该模型有等级、但还没选」。
   */
  const effortText = showEffort ? formatEffort(activeEffort) || '未选择' : '';

  // 当前 id 还在列表里时用展示名；被停用后列表里查不到，退回 id 尾段，至少与会话记录里
  // 保存的路由对得上。
  const modelLabel =
    selected?.name ||
    (currentModel ? currentModel.split('/').pop() || currentModel : '请选择模型');
  const rootRows = buildRootRows({ modelLabel, effortLabel: effortText });
  const triggerText = composeTriggerLabel(modelLabel, effortText);

  const groups = useMemo(
    () => orderProviderGroups(groupModelsByProvider(models)),
    [models],
  );
  const showSearch = shouldShowModelSearch(models.length);
  const filteredGroups = useMemo(
    () => filterModelGroups(groups, showSearch ? query : ''),
    [groups, query, showSearch],
  );
  const visibleModels = useMemo(
    () => filteredGroups.flatMap((group) => group.models),
    [filteredGroups],
  );

  const currentVisibleIndex = visibleModels.findIndex((model) => model.id === currentModel);
  const activeIndex = Math.min(
    highlighted ?? Math.max(0, currentVisibleIndex),
    Math.max(0, visibleModels.length - 1),
  );

  const busy = pending !== null;

  const close = useCallback((restoreFocus = false) => {
    setIsOpen(false);
    setPane('root');
    setQuery('');
    setHighlighted(null);
    setActionError(null);
    if (restoreFocus) {
      // 等菜单卸载后再聚焦：同一帧里它还在，浏览器可能把焦点算到菜单内部。
      queueMicrotask(() => triggerRef.current?.focus());
    }
  }, []);

  /** 选中后的收尾：焦点回触发器但不显示焦点环。 */
  const closeAfterSelection = useCallback(() => {
    setSelectionFocus(true);
    close(true);
  }, [close]);

  const show = useCallback(() => {
    setSelectionFocus(false);
    setActionError(null);
    setQuery('');
    setHighlighted(null);
    // 没有等级行就没有根面板可停：直接进模型列表，别让用户多点一次。
    // 有根面板时焦点留在触发器上——按下 ↑/↓（或 Tab）再进菜单。
    focusIntent.current = showEffort ? null : 'drill';
    setPane(showEffort ? 'root' : 'model');
    setIsOpen(true);
    triggerRef.current?.focus();
  }, [showEffort]);

  const drill = useCallback((next: BackTarget) => {
    setQuery('');
    setHighlighted(null);
    focusIntent.current = 'drill';
    setPane(next);
  }, []);

  /** 从下钻面板退回根面板，并把焦点交给打开它的那一行。 */
  const back = useCallback((from: BackTarget) => {
    focusIntent.current = from;
    setPane('root');
  }, []);

  /** 当前面板里可聚焦的菜单项（组标题与禁用项不算）。 */
  const focusItems = useCallback((): HTMLElement[] => {
    const root = menuRef.current;
    if (!root) return [];
    return Array.from(root.querySelectorAll<HTMLElement>('[data-menu-item]:not(:disabled)'));
  }, []);

  /** 面板里「已选中」的那一项：下钻时优先把焦点放在它上面。 */
  const focusChecked = useCallback((): HTMLElement | null => {
    const root = menuRef.current;
    if (!root) return null;
    return root.querySelector<HTMLElement>(
      '[role="menuitemradio"][aria-checked="true"]:not(:disabled)',
    );
  }, []);

  const moveFocus = useCallback(
    (delta: 1 | -1) => {
      const items = focusItems();
      if (items.length === 0) return;
      const current = items.findIndex((item) => item === document.activeElement);
      const next = cycleIndex(current === -1 ? null : current, delta, items.length);
      items[next]?.focus();
    },
    [focusItems],
  );

  const changeQuery = useCallback((next: string) => {
    setQuery(next);
    // 改搜索词后高亮第一个结果：方向键从第一条开始数才符合直觉
    setHighlighted(0);
  }, []);

  /**
   * 提交选择。
   *
   * 同步回调直接收尾；返回 Promise 时保持菜单打开，用它区分成功与「被拒绝」。
   */
  const submit = useCallback(
    (run: () => void | Promise<unknown>, marker: PendingSelection) => {
      setActionError(null);
      setSelectionFocus(true);
      triggerRef.current?.focus();

      let result: unknown;
      try {
        result = run();
      } catch (error) {
        setActionError(`模型操作失败：${describeFailure(error)}`);
        return;
      }

      if (!isThenable(result)) {
        closeAfterSelection();
        return;
      }

      setPending(marker);
      void Promise.resolve(result).then(
        () => {
          setPending(null);
          if (containerRef.current !== null) closeAfterSelection();
        },
        (error: unknown) => {
          // 被拒绝时菜单保持打开：用户需要「再选一次」的机会，而不是回头重开
          setPending(null);
          setSelectionFocus(false);
          setActionError(`模型操作失败：${describeFailure(error)}`);
        },
      );
    },
    [closeAfterSelection],
  );

  const chooseModel = useCallback(
    (model: ModelItem) => {
      if (busy) return;
      if (model.id === currentModel) {
        closeAfterSelection();
        return;
      }
      submit(() => onSelectModel(model.id), { kind: 'model', modelId: model.id });
    },
    [busy, closeAfterSelection, currentModel, onSelectModel, submit],
  );

  /** 搜索框里 Enter / Tab 的「选定高亮项」。定义在 `chooseModel` 之后，直接调用即可。 */
  const acceptHighlighted = useCallback(() => {
    const target = visibleModels[activeIndex];
    if (!busy && target) chooseModel(target);
  }, [activeIndex, busy, chooseModel, visibleModels]);

  const chooseEffort = useCallback(
    (level: string) => {
      if (busy) return;
      // 选的就是当前这一项：不提交（那会白写一次库），只收菜单。
      // 比较归一化后的值：父层存的可能是 `none`，而这里给的是 `off`。
      if (level === (normalizeEffort(currentReasoningEffort) ?? '')) {
        closeAfterSelection();
        return;
      }
      submit(() => onSelectEffort(level), { kind: 'effort', effort: level });
    },
    [busy, closeAfterSelection, currentReasoningEffort, onSelectEffort, submit],
  );

  const handleRetry = useCallback(async () => {
    // 用 ref 而不是 isRetrying 挡连点：setState 是异步的，同一帧连点两下都会看到
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
  }, [onRefreshModels]);

  // 面板切换（打开 / 下钻 / 返回）时安置焦点。只在面板变化时跑，因此改搜索词不会把焦点
  // 从输入框里抢走——搜索框持有焦点、↑/↓ 只移动高亮，是 DSH 的核心约束。
  useEffect(() => {
    if (!isOpen) return;
    const intent = focusIntent.current;
    focusIntent.current = null;
    // 新打开的根面板：焦点留在触发器上，等用户按 ↑/↓ 再进来
    if (intent === null) return;

    if (intent === 'drill') {
      // 搜索框持有焦点是下钻模型面板的既定行为；没有搜索框（目录小）时落到当前项
      if (pane === 'model' && showSearch) {
        searchRef.current?.focus();
        return;
      }
      (focusChecked() ?? focusItems()[0] ?? triggerRef.current)?.focus();
      return;
    }

    // 返回根面板：焦点交给打开它的那一行
    const rowIndex = intent === 'effort' ? 1 : 0;
    (focusItems()[rowIndex] ?? focusItems()[0] ?? triggerRef.current)?.focus();
  }, [isOpen, pane, showSearch, focusChecked, focusItems]);

  // 高亮项滚进可视区。按 DOM 顺序取，不维护 ref 数组——分组随搜索变化，索引数组最容易
  // 在「搜索后条目变少」时留下越界的陈旧引用。
  useEffect(() => {
    if (!isOpen || pane !== 'model' || !showSearch) return;
    const options = menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitemradio"]');
    options?.[activeIndex]?.scrollIntoView({ block: 'nearest' });
  }, [isOpen, pane, showSearch, activeIndex, visibleModels]);

  // 点击外部关闭。pointerdown 而不是 click：按下就关，符合用户对菜单的预期。
  useEffect(() => {
    if (!isOpen) return undefined;

    const handlePointerDown = (event: PointerEvent) => {
      if (containerRef.current?.contains(event.target as Node)) return;
      close(false);
    };

    document.addEventListener('pointerdown', handlePointerDown);
    return () => document.removeEventListener('pointerdown', handlePointerDown);
  }, [isOpen, close]);

  const handleBlur = (event: React.FocusEvent<HTMLDivElement>) => {
    // Tab 把焦点移出容器（含移出到浏览器 UI：relatedTarget 为 null）时关闭。
    const next = event.relatedTarget;
    if (isOpen && !(next instanceof Node && containerRef.current?.contains(next))) close(false);
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.nativeEvent.isComposing) return;
    const { key } = event;

    if (!isOpen) {
      if (key === 'ArrowDown' || key === 'ArrowUp') {
        event.preventDefault();
        show();
      }
      return;
    }

    if (key === 'Escape') {
      event.preventDefault();
      // 先退回根面板（存在根面板时），再退才关闭并回触发器
      if (pane !== 'root' && showEffort) {
        back(pane as BackTarget);
        return;
      }
      // 别把 Esc 漏给外层：那个 Esc 是「停止生成」
      event.stopPropagation();
      close(true);
      return;
    }

    if (key === 'Tab') {
      if (event.shiftKey) {
        event.preventDefault();
        if (pane !== 'root' && showEffort) back(pane as BackTarget);
        else close(true);
        return;
      }

      const focused = document.activeElement;
      const onItem = focused instanceof HTMLElement && focusItems().includes(focused);
      if (onItem) {
        // 焦点已落在菜单项上：Tab 选定它（与搜索框内 Tab 的语义保持一致）
        event.preventDefault();
        focused.click();
        return;
      }

      if (focused === triggerRef.current) {
        event.preventDefault();
        if (pane === 'model' && showSearch) {
          setHighlighted(currentVisibleIndex >= 0 ? currentVisibleIndex : 0);
          searchRef.current?.focus();
          return;
        }
        (focusChecked() ?? focusItems()[0])?.focus();
      }
      return;
    }

    if (key === 'ArrowDown' || key === 'ArrowUp') {
      const delta = key === 'ArrowDown' ? 1 : -1;
      // 搜索框持有焦点时只移动高亮，焦点留在输入框（←/→ 还要用来移光标）
      if (pane === 'model' && showSearch && document.activeElement === searchRef.current) {
        event.preventDefault();
        if (!busy && visibleModels.length > 0) {
          setHighlighted(cycleIndex(activeIndex, delta, visibleModels.length));
        }
        return;
      }
      event.preventDefault();
      moveFocus(delta);
    }
  };

  /** 搜索框自己的键盘：Enter / Tab 选定；↑/↓ 由容器的 handleKeyDown 处理。 */
  const handleSearchKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing) return;
    const { key } = event;

    if (key === 'Enter') {
      event.preventDefault();
      acceptHighlighted();
      return;
    }
    if (key === 'Tab' && !event.shiftKey) {
      // Tab 在这里是「选定」而不是「离开」：焦点本来就停在搜索框上，让它跳走等于用户在
      // 还没选定时就丢了菜单。
      if (visibleModels.length === 0) return;
      event.preventDefault();
      acceptHighlighted();
    }
  };

  return (
    <div
      ref={containerRef}
      className="relative inline-block min-w-0"
      onKeyDown={handleKeyDown}
      onBlur={handleBlur}
    >
      <button
        ref={triggerRef}
        type="button"
        onClick={() => (isOpen ? close(true) : show())}
        aria-haspopup="menu"
        aria-expanded={isOpen}
        aria-controls={isOpen ? `${id}-menu` : undefined}
        aria-busy={busy}
        /* 两段文字是分色渲染的，不合并成一个名字就会被读成两段互不相干的文本；当前模型
           被停用时也把结论念出来——颜色是唯一的视觉线索，读屏用户看不到。 */
        aria-label={`${showEffort ? `${modelLabel}，${effortText}` : modelLabel}${
          selectedMissing ? '（当前模型不可用）' : ''
        }`}
        title={triggerText}
        data-selection-focus={selectionFocus ? '' : undefined}
        /* 焦点离开触发器（或菜单重开）后就恢复正常焦点提示：那枚「静默」只服务于
           「刚选完、焦点被归还」这一小段时间。 */
        onBlur={() => setSelectionFocus(false)}
        className={`flex h-7 min-w-0 max-w-full cursor-pointer items-center gap-1 rounded-sm bg-transparent pr-1 pl-2 text-[13px] leading-5 font-normal transition-colors duration-100 hover:bg-accent pointer-coarse:min-h-9 ${
          // 选定后焦点回到触发器但不显示焦点环：那圈环的含义是「菜单开着、键盘停在这里」，
          // 而此刻菜单已经关了。
          selectionFocus
            ? 'outline-none'
            : 'focus-visible:ring-2 focus-visible:ring-ring/60 focus-visible:outline-none'
        }`}
      >
        {/* 当前模型已被停用：模型名给一点琥珀色。菜单里的告警要打开才看得见，而
            「下一条消息会被拒」这件事在闭合状态就该有线索。 */}
        <span
          className={`min-w-0 truncate ${
            selectedMissing ? 'text-amber-700 dark:text-amber-400' : 'text-muted-foreground'
          }`}
        >
          {modelLabel}
        </span>
        {showEffort && (
          /* 等级用更淡的 caption 色：它是模型的附属信息，不该和模型名抢注意力。窄屏先
             牺牲这段（模型名有 min-w-0 且不被 shrink-[1000]），模型名才是「选了什么」的证据。 */
          <span className="min-w-0 shrink-[1000] truncate text-muted-foreground/60">
            · {effortText}
          </span>
        )}
        {busy ? (
          <Loader2
            size={12}
            className="shrink-0 animate-spin text-muted-foreground"
            aria-hidden="true"
          />
        ) : (
          <ChevronDown
            size={12}
            className={`shrink-0 text-muted-foreground/70 transition-transform duration-100 ${
              isOpen ? 'rotate-180' : ''
            }`}
            aria-hidden="true"
          />
        )}
      </button>

      {isOpen && (
        <div
          ref={menuRef}
          id={`${id}-menu`}
          role={pane === 'model' ? 'group' : 'menu'}
          aria-label="模型与思考等级"
          aria-busy={busy}
          className="absolute bottom-full left-0 z-50 mb-1.5 flex max-h-[min(360px,calc(100vh_-_96px))] w-max min-w-[240px] max-w-[min(420px,calc(100vw_-_1.5rem))] flex-col overflow-hidden rounded-xl border border-border bg-popover p-1 text-xs text-popover-foreground shadow-xl"
          // 菜单项上按下鼠标不抢焦点：搜索框要保持聚焦，否则点一下结果就把输入法和
          // 光标丢了，想接着改搜索词还得点回去。选中动作由 click 完成，preventDefault
          // 不影响它；这里只拦按钮，不能拦滚动条（拖它要靠默认的 mousedown）。
          onMouseDown={(event) => {
            if (event.target instanceof Element && event.target.closest('button')) {
              event.preventDefault();
            }
          }}
        >
          {actionError && (
            <div
              role="alert"
              className="mb-0.5 rounded-lg border border-destructive/40 bg-destructive/10 px-2 py-1.5 text-[11px] leading-4 text-destructive"
            >
              {actionError}
            </div>
          )}

          {pane === 'root' && (
            <div className="flex flex-col" role="none">
              {rootRows.map((row) => (
                <button
                  key={row.key}
                  type="button"
                  role="menuitem"
                  data-menu-item="true"
                  onClick={() => drill(row.key)}
                  className="flex h-[34px] w-full min-w-full cursor-pointer items-center gap-1.5 rounded-lg px-2 text-left text-[13px] leading-5 text-popover-foreground outline-none hover:bg-accent focus-visible:bg-accent"
                >
                  <span className="shrink-0 whitespace-nowrap">{row.label}</span>
                  <span className="min-w-0 flex-auto truncate text-right text-muted-foreground">
                    {row.value}
                  </span>
                  <ChevronRight
                    size={12}
                    className="shrink-0 text-muted-foreground/70"
                    aria-hidden="true"
                  />
                </button>
              ))}
            </div>
          )}

          {pane === 'model' && (
            <>
              {showSearch && (
                <div className="relative mb-0.5 shrink-0">
                  <input
                    ref={searchRef}
                    type="text"
                    role="searchbox"
                    aria-label="搜索模型"
                    aria-controls={`${id}-models`}
                    /* 焦点始终留在搜索框，高亮项只能靠 aria-activedescendant 播报——
                       tabIndex=-1 的选项不会被读屏当成「当前项」。 */
                    aria-activedescendant={
                      visibleModels.length > 0 ? `${id}-model-${activeIndex}` : undefined
                    }
                    placeholder="搜索模型…"
                    value={query}
                    readOnly={busy}
                    onChange={(event) => changeQuery(event.target.value)}
                    onKeyDown={handleSearchKeyDown}
                    className={`h-auto w-full min-w-0 rounded-lg border-0 bg-transparent px-2 py-1 text-xs leading-normal outline-none placeholder:text-muted-foreground/60 ${
                      query !== '' ? 'pr-8' : ''
                    }`}
                  />
                  {query !== '' && (
                    <button
                      type="button"
                      aria-label="清除搜索"
                      disabled={busy}
                      onClick={() => {
                        changeQuery('');
                        searchRef.current?.focus();
                      }}
                      className="absolute top-1/2 right-1 flex size-6 -translate-y-1/2 cursor-pointer items-center justify-center rounded-full text-muted-foreground hover:bg-accent focus-visible:bg-accent focus-visible:outline-none"
                    >
                      <X size={12} aria-hidden="true" />
                    </button>
                  )}
                </div>
              )}

              {/* 管理员停用了当前模型：列表里已经没有它，继续发请求只会拿到
                  model_unavailable。给出结论和一条出路（默认模型仍在时一键切过去）。 */}
              {selectedMissing && (
                <div
                  role="status"
                  className="mb-1 rounded-lg bg-amber-500/10 px-2 py-1.5 text-[11px] leading-4 text-amber-700 dark:text-amber-400"
                >
                  <p>当前模型已不在可用列表，可能已被管理员停用；继续发送会被服务端拒绝。</p>
                  {defaultModel && (
                    <button
                      type="button"
                      onClick={() => chooseModel(defaultModel)}
                      className="mt-1 inline-flex cursor-pointer items-center gap-1 rounded-md border border-amber-500/40 bg-background/70 px-1.5 py-0.5 font-medium text-foreground hover:bg-background"
                    >
                      切到默认模型：{defaultModel.name}
                    </button>
                  )}
                </div>
              )}

              {isEmpty ? (
                <div className="flex flex-col items-start gap-2 px-2 py-2 text-[12px] leading-[18px] text-muted-foreground">
                  <div className="flex items-start gap-1.5">
                    <AlertCircle size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
                    <p className="min-w-0 break-words">
                      {emptyCatalogText({ modelsError, loading: isLoading || isRetrying })}
                    </p>
                  </div>
                  {onRefreshModels && (
                    <button
                      type="button"
                      onClick={() => void handleRetry()}
                      disabled={isRetrying}
                      className="inline-flex cursor-pointer items-center gap-1.5 rounded-md border border-border/60 px-2 py-0.5 text-[11px] text-foreground/80 transition hover:bg-accent disabled:cursor-default disabled:opacity-60"
                    >
                      <RefreshCw
                        size={11}
                        className={isRetrying ? 'animate-spin' : ''}
                        aria-hidden="true"
                      />
                      {isRetrying ? '刷新中…' : '重试'}
                    </button>
                  )}
                </div>
              ) : (
                <div
                  id={`${id}-models`}
                  role="menu"
                  aria-label="模型"
                  className="min-h-0 flex-1 overflow-y-auto"
                  hidden={filteredGroups.length === 0}
                >
                  {filteredGroups.map((group) => (
                    /* 每个提供方是一个 group：role=menu 的直接子节点只该是 option 或
                       group，中间再夹一层没有角色的 div 会让读屏的「第 N 项，共 M 项」
                       数错。 */
                    <div key={group.name} role="group" aria-label={group.name}>
                      {/*
                        组标题吸顶。常驻 94% 的 popover 底而不是「原位透明、吸顶才上色」：
                        94% 的 popover 叠在 popover 上几乎就是原色，未吸顶时与不透明底看
                        不出差别；一旦滚到行上面，剩下 6% 的透出又刚好被下面的行盖住。
                        省掉一个会抖的滚动观察器（DSH 用 observeStickyMenuGroups 做同一
                        件事，代价是每个分组一个 IntersectionObserver）。
                      */}
                      <div className="sticky top-0 z-10 bg-popover/[0.94] px-2 py-1 text-[11px] leading-4 text-muted-foreground">
                        {group.name}
                      </div>
                      {group.models.map((model) => {
                        const index = visibleModels.indexOf(model);
                        const isSelected = currentModel === model.id;
                        const isActive = index === activeIndex;
                        const isPending =
                          pending?.kind === 'model' && pending.modelId === model.id;
                        return (
                          <button
                            key={model.id}
                            type="button"
                            id={`${id}-model-${index}`}
                            role="menuitemradio"
                            aria-checked={isSelected}
                            data-menu-item="true"
                            tabIndex={showSearch ? -1 : 0}
                            disabled={busy}
                            title={modelFullLabel(model.name, model)}
                            onMouseMove={(event) => {
                              if (busy) return;
                              if (showSearch) {
                                if (index !== activeIndex) setHighlighted(index);
                              } else if (document.activeElement !== event.currentTarget) {
                                // 没有搜索框时焦点就是「高亮」：鼠标扫过要把它带过去，
                                // 否则 Enter 会选中键盘停着的那一行，和眼前的对不上。
                                event.currentTarget.focus();
                              }
                            }}
                            onClick={() => chooseModel(model)}
                            className={`flex min-h-[34px] w-full min-w-full cursor-pointer items-center gap-1.5 rounded-lg px-2 py-1 text-left outline-none hover:bg-accent focus-visible:bg-accent disabled:cursor-default disabled:text-muted-foreground/60 ${
                              isActive ? 'bg-accent' : ''
                            }`}
                          >
                            {/* 勾选位固定 14px 且常驻（没选中时留空位）：行首图标列不跟着
                                选中状态左右跳，模型名才能在一条竖线上对齐。 */}
                            <span className="grid w-3.5 shrink-0 place-items-center text-popover-foreground">
                              {isPending ? (
                                <Loader2 size={14} className="animate-spin" aria-hidden="true" />
                              ) : isSelected ? (
                                <Check size={14} aria-hidden="true" />
                              ) : null}
                            </span>
                            <span className="flex min-w-0 flex-1 flex-col">
                              <span className="truncate text-[13px] leading-[18px] font-normal">
                                {model.name}
                                {/* DSH 的选项行只显示模型名；但本机确实存在三条展示名
                                    完全相同的路由（auto/ workbuddy/ workbuddy-ai/），
                                    读屏用户只有靠这里才能区分。视觉保持干净，悬停有
                                    title。 */}
                                {modelSubtitle(model) ? (
                                  <span className="sr-only">（{modelSubtitle(model)}）</span>
                                ) : null}
                              </span>
                            </span>
                          </button>
                        );
                      })}
                    </div>
                  ))}
                </div>
              )}

              {!isEmpty && filteredGroups.length === 0 && (
                <div
                  className="px-2 py-2 text-[12px] leading-[18px] text-muted-foreground"
                  role="status"
                >
                  没有匹配的模型。
                </div>
              )}
            </>
          )}

          {pane === 'effort' && (
            <div role="none">
              {levels.length === 0 ? (
                <div className="px-2 py-2 text-[12px] leading-[18px] text-muted-foreground">
                  当前模型未提供思考等级。
                </div>
              ) : (
                levels.map((level) => {
                  const isSelected = level === activeEffort;
                  const isPending = pending?.kind === 'effort' && pending.effort === level;
                  return (
                    <button
                      key={level}
                      type="button"
                      role="menuitemradio"
                      aria-checked={isSelected}
                      data-menu-item="true"
                      disabled={busy}
                      onClick={() => chooseEffort(level)}
                      className="flex min-h-[34px] w-full min-w-full cursor-pointer items-center gap-1.5 rounded-lg px-2 py-1 text-left outline-none hover:bg-accent focus-visible:bg-accent disabled:cursor-default disabled:text-muted-foreground/60"
                    >
                      <span className="grid w-3.5 shrink-0 place-items-center text-popover-foreground">
                        {isPending ? (
                          <Loader2 size={14} className="animate-spin" aria-hidden="true" />
                        ) : isSelected ? (
                          <Check size={14} aria-hidden="true" />
                        ) : null}
                      </span>
                      <span className="flex min-w-0 flex-1 flex-col">
                        <span className="truncate text-[13px] leading-[18px] font-normal">
                          {formatEffort(level)}
                        </span>
                      </span>
                    </button>
                  );
                })
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
