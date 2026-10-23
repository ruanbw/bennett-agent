import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { useAuiState } from "@assistant-ui/react";
import { usePresentationPolicy } from "@/context/detail-level-context";
import { cn } from "@/lib/utils";
import {
  BookOpen,
  Bot,
  Brain,
  ChevronDown,
  ChevronUp,
  Clock,
  Code,
  Compass,
  Globe,
  Image as ImageIcon,
  ListChecks,
  Loader2,
  MessageCircleQuestion,
  PencilLine,
  Search,
  Sparkles,
  SquareTerminal,
} from "lucide-react";
import {
  type FC,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { classifyActivity, summarizeActivities, type ActivityKind } from "@/lib/step-process";
import { liveToolDetail, traceGroupHeader, traceToggleLabel } from "@/lib/trace-summary";
import { resolveToolCallViewStatus } from "@/lib/tool-call-view";

/**
 * 「过程」折叠块：把**思考**与**工具调用**一起收成一行，外面只留最终回答。
 *
 * 这一层是整条消息的分界线：折叠条以上的东西（想了多久、调了什么工具、读了哪些文件）
 * 都属于「怎么得出答案」，不是答案本身。收进同一个可点目标后，一轮回答在页面上只剩
 * 两样东西——一行过程摘要，和正文。
 *
 * 外观对齐 DSH 的 `ChatGroupSeat`：一条**没有边框**的可点文字行，左边是活动图标，
 * hover 或展开时图标淡出、换成 chevron（展开朝上）。此前本仓的样式是「左边一条竖线
 * + 带底色的小胶囊」，那会让过程组看起来像一块独立卡片，和正文抢视觉重量。
 *
 * 关于「默认收起」：连生成中也不自动摊开。用户要的是「只看到最终回答」，生成期间
 * 自己会点开看进度；强行展开会让每轮对话都被一段过程占满。这一条**不**随档位改变，
 * 唯一例外是「详细」档下**历史**轮次保持展开（见 `lib/detail-level` 的策略表）。
 */

/** 活动图标：与 DSH 的 `PROCESS_ICONS` 一一对应（同一分类用同一个图标）。 */
const ACTIVITY_ICONS: Record<ActivityKind, typeof Brain> = {
  thinking: Brain,
  read: Compass,
  readImage: ImageIcon,
  write: PencilLine,
  search: Search,
  edit: PencilLine,
  commands: SquareTerminal,
  code: Code,
  webSearch: Globe,
  webFetch: Compass,
  time: Clock,
  knowledge: BookOpen,
  subagents: Bot,
  plan: ListChecks,
  questions: MessageCircleQuestion,
  tools: Sparkles,
};

/**
 * 组内容的限高：`min(400px, 50vh)`。
 *
 * 短过程自然展开；长过程（几十个工具调用）滚动而不是把消息列表撑破。
 * 「完全展开」档根本不走这条路径（见下面的内联分支），所以这里不需要再判断。
 */
const GROUP_BODY_MAX_HEIGHT = "min(400px, 50vh)";

export const TraceGroup: FC<{
  /** 组内部件下标（来自 `MessagePrimitive.GroupedParts`）。 */
  indices: readonly number[];
  /** 组内各状态的计数（来自 `GroupedParts` 的 counts）。 */
  counts: { running: number; complete: number; incomplete: number; requiresAction: number };
  /**
   * 这一组是否属于**已结束**的一轮。默认由消息自身的状态推导（见 `settledNow`），
   * 显式传入只用于改写那个推导（测试、或将来出现「状态不可信」的场景）。
   *
   * 「详细」档只让历史轮次保持展开：进行中的组一旦默认摊开，流式增量的插入会让
   * 高度反复跳动，用户反而看不清进度。
   */
  settled?: boolean;
  children: ReactNode;
}> = ({ indices, counts, settled: settledProp, children }) => {
  const policy = usePresentationPolicy();
  const [userToggled, setUserToggled] = useState<boolean | null>(null);

  const parts = useAuiState((s) => s.message.parts);
  /**
   * 这一轮是否已经结束。
   *
   * 从消息状态推导而不是由调用方传：`GroupedParts` 的 children 是**渲染函数**而不是
   * 组件，在那里读 `useAuiState` 会违反 hook 规则（同一个函数既可能渲染分组也可能渲染
   * 单个部件）；而在调用处硬编码只会得到「所有消息都已结束」——进行中的轮次在「详细」
   * 档会被误展开，正是这条规则要避免的事。
   *
   * 只有 `running` 算未结束：`requires-action`（等审批/等回答）是**停下来了**等用户，
   * 此时过程应当稳定展开，不该跟着流继续跳。
   */
  const settledNow = useAuiState((s) => s.message.status?.type !== "running");
  const settled = settledProp ?? settledNow;
  const selected = useMemo(
    () => indices.map((index) => parts[index]).filter((part) => part !== undefined),
    [indices, parts],
  );

  const toolParts = useMemo(
    () => selected.filter((part) => part.type === "tool-call"),
    [selected],
  );
  // 运行中要展示「当前在干什么」：取最后一个仍在跑的调用。没有在跑的工具时
  // 说明这一组只有思考，活动分类就是 thinking。
  const lastRunning = useMemo(() => {
    for (let index = toolParts.length - 1; index >= 0; index -= 1) {
      const part = toolParts[index]!;
      if (part.status?.type === "running") return part;
    }
    return undefined;
  }, [toolParts]);

  const running = counts.running > 0 || lastRunning !== undefined;
  const activity: ActivityKind = lastRunning ? classifyActivity(lastRunning.toolName) : "thinking";

  // 参数还在流式生成时 argsText 不是合法 JSON，`liveToolDetail` 给空串——
  // 此时只显示动作标题，不显示半截 `{"que`。
  const liveDetail = lastRunning ? liveToolDetail(lastRunning.argsText) : "";

  const toolNames = useMemo(() => toolParts.map((part) => part.toolName), [toolParts]);

  // 失败/被拒绝/中断都算「出了问题」：折叠着也要能看出这轮不顺利，
  // 不能只靠展开后逐个看图标，也不能只靠颜色（色弱读不出）。
  const failedCount = toolParts.filter((part) => {
    const viewStatus = resolveToolCallViewStatus(part.status, part.result);
    return viewStatus === "failed" || viewStatus === "denied" || viewStatus === "interrupted";
  }).length;

  /**
   * 组头文字（纯逻辑在 `lib/trace-summary.traceGroupHeader`，便于单测）。
   *
   * 运行中：「正在运行命令」，标准/详细/完全展开档再补 ` · npm test`。
   * 已结束：「已读取文件，执行了命令」——不带次数，也不带耗时（耗时属于整轮的时长行）。
   */
  const { title, summary, detail, failureNote } = traceGroupHeader({
    running,
    activity,
    detail: liveDetail,
    toolNames,
    failedCount,
    showLiveDetail: policy.liveProcessDetail,
  });

  /**
   * 默认展开规则：
   * - 用户点过 → 以用户为准（本轮内有效）
   * - 没点过 → 只有「详细」档的**历史**轮次默认展开
   */
  const defaultOpen = policy.stepGrouping === "history" && settled;
  const isOpen = userToggled ?? defaultOpen;

  /**
   * 可滚动方向（决定加哪边的渐隐遮罩）。
   *
   * 用 state 而不是纯 CSS：遮罩只在「确实还能往那个方向滚」时才有意义。
   * `overflow-y-auto` 本身不告诉我们内容是否溢出，只能靠量。
   */
  const bodyRef = useRef<HTMLDivElement>(null);
  const [edges, setEdges] = useState({ up: false, down: false });

  const updateEdges = useCallback(() => {
    const el = bodyRef.current;
    if (!el) return;
    // 1px 容差：分数缩放（125%/150%）下 scrollTop 与 scrollHeight 常有亚像素差，
    // 严格相等会让「已经到顶」被判成还能往上滚，遮罩就一直挂着。
    const tolerance = 1;
    const up = el.scrollTop > tolerance;
    const down = el.scrollTop + el.clientHeight < el.scrollHeight - tolerance;
    setEdges((prev) => (prev.up === up && prev.down === down ? prev : { up, down }));
  }, []);

  // 展开、内容变化、窗口尺寸变化都可能改变「能不能滚」，三种时机都要重量一次。
  // 不用 `children` 当依赖：它每次渲染都是新引用，会让 observer 每帧重建。
  // ResizeObserver 观察的是滚动容器本身，容器高度变化（内容增多/窗口缩放）都会回调，
  // 已经把内容变化覆盖住了。
  useEffect(() => {
    if (!isOpen) return undefined;
    updateEdges();
    const el = bodyRef.current;
    if (!el || typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(() => updateEdges());
    observer.observe(el);
    return () => observer.disconnect();
  }, [isOpen, updateEdges, settled]);

  // 「完全展开」档：过程行直接铺开，不再有可折叠的组，也就没有组头。
  // 不套限高——这一档的语义就是「让我看到全部」。
  if (policy.stepGrouping === "none") {
    return (
      <div className="mt-1.5 mb-1 flex w-full flex-col gap-1.5 select-none" data-trace-mode="inline">
        {children}
      </div>
    );
  }

  // 组头图标：运行中按当前动作，已结束按最主要的那个动作（与文字首项一致）
  const settledActivity = summarizeActivities(toolNames)[0]?.kind;
  const Icon = ACTIVITY_ICONS[running ? activity : (settledActivity ?? "thinking")] ?? Sparkles;

  return (
    <Collapsible
      open={isOpen}
      onOpenChange={setUserToggled}
      className="mt-1.5 mb-1 w-full select-none"
    >
      {/*
        组头：一行无边框文字（DSH 的 ChatGroupSeat.title）。
        - tertiary 色 hover 变 secondary：它比正文弱，但仍可读
        - 展开时 padding-bottom 8px：给组头和内容之间留出呼吸，否则紧贴在一起
        - 左侧「hover 或展开时图标淡出换 chevron」用两层绝对定位叠放实现
      */}
      <CollapsibleTrigger
        render={
          <button
            type="button"
            aria-label={traceToggleLabel(`${title}${failureNote}`, isOpen)}
            className={cn(
              "group/trace flex max-w-full cursor-pointer items-center gap-1.5 rounded-md text-left text-sm text-muted-foreground transition-colors",
              "hover:text-secondary-foreground focus-visible:ring-1 focus-visible:ring-ring/50 focus-visible:outline-none",
              isOpen ? "pb-2" : "pb-0",
            )}
          />
        }
      >
        {/* 图标位：16px 定宽，活动图标与 chevron 叠在同一格里 */}
        <span className="relative inline-flex size-4 shrink-0 items-center justify-center">
          <span
            className={cn(
              "absolute inset-0 inline-flex items-center justify-center transition-opacity duration-100",
              isOpen ? "opacity-0" : "opacity-100 group-hover/trace:opacity-0",
            )}
            aria-hidden="true"
          >
            <Icon className="size-3.5" />
          </span>
          <span
            className={cn(
              "absolute inset-0 inline-flex items-center justify-center transition-opacity duration-100",
              isOpen ? "opacity-100" : "opacity-0 group-hover/trace:opacity-100",
            )}
            aria-hidden="true"
          >
            {isOpen ? <ChevronUp className="size-3.5" /> : <ChevronDown className="size-3.5" />}
          </span>
        </span>
        <span className="truncate">
          {/* 运行中的文字用 shimmer 扫光（DSH 的 TextShimmer）：这是「它还在动」的
              主要视觉线索，比一个转圈图标更贴近「文字正在被写出来」的语义。 */}
          <span className={cn(running && "shimmer motion-reduce:animate-none")}>{summary}</span>
          {detail !== "" && <span className="text-muted-foreground/70"> · {detail}</span>}
          {failureNote}
        </span>
        {/* 运行中的 spinner：图标被 chevron 遮住时，「还在动」这件事不能只靠文字 */}
        {running && (
          <Loader2
            className="size-3 shrink-0 animate-spin text-muted-foreground/70"
            aria-hidden="true"
          />
        )}
      </CollapsibleTrigger>

      {/* base-ui 的 Panel 只挂 data-open/data-closed，不是 Radix 的 data-state */}
      <CollapsibleContent>
        {/*
          组内容：行间距 6px（DSH 的 --dsh-chat-flow-gap），限高可滚动。
          展开后**仍然**限高：固定高度让「点开一眼扫完」成立，无限高会让长过程把
          后续内容推到几屏之外——DSH 同样如此（只有不分组的「完全展开」档才不限高）。

          上下 24px 的渐隐用**两个静止的覆盖层**，不用 mask-image：mask 作用在内容
          自身的 alpha 上，代价是整棵子树被合成成一层、滚动与展开时逐帧重绘——而这里
          装的正好是最贵的那棵子树（几十条工具行）。覆盖层不随内容滚动，只画一次，
          视觉上等价：只有真的还能往那个方向滚时才画，滚到底就撤掉。

          不用 `overscroll-contain`：那会让滚动停在组内。指针停在组上继续滚页面时，
          内层到底后页面不动，看起来就是「滚卡住了」。允许滚动链，内层到底自然接上。
        */}
        <div className="relative">
          <div
            ref={bodyRef}
            onScroll={updateEdges}
            className="flex flex-col gap-1.5 overflow-y-auto [scrollbar-gutter:stable]"
            style={{ maxHeight: GROUP_BODY_MAX_HEIGHT }}
            data-trace-body=""
          >
            {children}
          </div>
          {edges.up && (
            <div
              aria-hidden="true"
              className="pointer-events-none absolute inset-x-0 top-0 h-6 bg-gradient-to-b from-background to-transparent"
            />
          )}
          {edges.down && (
            <div
              aria-hidden="true"
              className="pointer-events-none absolute inset-x-0 bottom-0 h-6 bg-gradient-to-t from-background to-transparent"
            />
          )}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
};
