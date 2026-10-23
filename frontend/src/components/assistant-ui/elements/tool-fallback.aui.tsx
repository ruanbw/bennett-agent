"use client";

import { memo, useCallback, useMemo, useState } from "react";
import {
  AlertCircleIcon,
  BanIcon,
  BookOpen,
  Bot,
  Brain,
  CheckIcon,
  ChevronDownIcon,
  CircleMinusIcon,
  Clock,
  Code,
  Compass,
  Globe,
  Image as ImageIcon,
  ListChecks,
  LoaderIcon,
  MessageCircleQuestion,
  PencilLine,
  Search,
  Sparkles,
  SquareTerminal,
  XCircleIcon,
} from "lucide-react";
import {
  useToolCallElapsed,
  type ToolCallMessagePartComponent,
  type ToolCallMessagePartStatus,
} from "@assistant-ui/react";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { cn } from "@/lib/utils";
import {
  formatToolDuration,
  formatUnknownValue,
  failedToolResult,
  previewToolText,
  resolveToolCallViewStatus,
  toolArgsPreview,
  toolTextLength,
  type ToolCallViewStatus,
} from "@/lib/tool-call-view";
import { classifyActivity, toolRowTitle, type ActivityKind } from "@/lib/step-process";

export type ToolFallbackRootProps = Omit<
  React.ComponentProps<typeof Collapsible>,
  "open" | "onOpenChange"
> & {
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  defaultOpen?: boolean;
};

/**
 * 工具调用的折叠单元。
 *
 * 这里**刻意不**接 assistant-ui 的 `useScrollLock`：它会在每次展开时找到最近的
 * 可滚动祖先，把 `scrollbar-width` 设成 none、按滚动条宽度改 padding，并在 200ms 内
 * 把 scrollTop 钉回原位。三件事都发生在点击的那一帧——强制重排、滚动条闪烁，
 * 期间用户也滚不动。高度过渡改成真正的 transition 之后（见 `src/index.css`），
 * 内容高度是连续变化的，浏览器自己的滚动锚定足以处理，不需要这个补丁。
 */
function ToolFallbackRoot({
  className,
  open: controlledOpen,
  onOpenChange: controlledOnOpenChange,
  defaultOpen = false,
  children,
  ...props
}: ToolFallbackRootProps) {
  const [uncontrolledOpen, setUncontrolledOpen] = useState(defaultOpen);

  const isControlled = controlledOpen !== undefined;
  const isOpen = isControlled ? controlledOpen : uncontrolledOpen;

  const handleOpenChange = useCallback(
    (open: boolean) => {
      if (!isControlled) {
        setUncontrolledOpen(open);
      }
      controlledOnOpenChange?.(open);
    },
    [isControlled, controlledOnOpenChange],
  );

  return (
    <Collapsible
      data-slot="tool-fallback-root"
      open={isOpen}
      onOpenChange={handleOpenChange}
      className={cn(
        "aui-tool-fallback-root group/tool-fallback-root w-full",
        className,
      )}
      {...props}
    >
      {children}
    </Collapsible>
  );
}

/** 状态图标；interrupted 是本仓补的一档（没有结果就结束），见 lib/tool-call-view。 */
const statusIconMap: Record<ToolCallViewStatus, React.ElementType> = {
  running: LoaderIcon,
  complete: CheckIcon,
  incomplete: XCircleIcon,
  "requires-action": AlertCircleIcon,
  interrupted: CircleMinusIcon,
  failed: XCircleIcon,
  denied: BanIcon,
};

/**
 * 动作类型 → 行首图标。
 *
 * 与「过程」组头的图标表同源（`trace-group.tsx` 的 `ACTIVITY_ICONS`）：同一次调用
 * 在组头里是「正在运行命令」，在工具行里也必须还是那个终端图标，否则用户会以为是
 * 两件事。分组标题用的是名词（运行命令），组头用的是叙述（执行了命令）。
 */
const ACTIVITY_ICONS: Record<ActivityKind, React.ElementType> = {
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

/** 触发条的无障碍状态文案：可见文案只有「未完成」一档，这里给出完整语义。 */
const STATUS_ARIA_LABEL: Record<ToolCallViewStatus, string> = {
  running: "执行中",
  complete: "已完成",
  incomplete: "未完成",
  "requires-action": "等待处理",
  interrupted: "未完成，工具没有返回结果",
  failed: "执行失败",
  denied: "被拒绝",
};

function ToolFallbackDuration({
  className,
  ...props
}: React.ComponentProps<"span">) {
  const elapsedMs = useToolCallElapsed();
  if (elapsedMs === undefined) return null;

  return (
    <span
      data-slot="tool-fallback-duration"
      className={cn(
        "aui-tool-fallback-duration text-muted-foreground text-xs tabular-nums",
        className,
      )}
      {...props}
    >
      {formatToolDuration(elapsedMs)}
    </span>
  );
}

function ToolFallbackTrigger({
  toolName,
  argsText,
  status,
  result,
  className,
  ...props
}: React.ComponentProps<typeof CollapsibleTrigger> & {
  toolName: string;
  /** 原始参数文本；只用来生成折叠态的一行预览。 */
  argsText?: string;
  status?: ToolCallMessagePartStatus;
  result?: unknown;
}) {
  const viewStatus = resolveToolCallViewStatus(status, result);
  // 大参数对象在这里只解析一次；折叠态展示主参数，展开后仍看完整 args
  const preview = useMemo(() => toolArgsPreview(argsText), [argsText]);
  const isRunning = viewStatus === "running";
  const isInterrupted = viewStatus === "interrupted";
  const isFailed = viewStatus === "failed";
  const isDenied = viewStatus === "denied";
  const isCancelled =
    status?.type === "incomplete" && status.reason === "cancelled";

  // 行首图标按**动作类型**给（终端、放大镜、指南针…），而不是按状态给对勾/叉：
  // 状态已经在右侧的耗时位与失败文案里表达，图标位置留给「这是什么调用」。
  const ActivityIcon = ACTIVITY_ICONS[classifyActivity(toolName)] ?? Sparkles;
  const title = toolRowTitle(toolName);
  // 失败/被停止用状态图标覆盖动作图标：这两种是异常终态，必须一眼看出
  const StatusIcon = statusIconMap[viewStatus];
  const useStatusIcon = isFailed || isDenied || isInterrupted || isCancelled;
  const Icon = useStatusIcon ? StatusIcon : ActivityIcon;

  return (
    <CollapsibleTrigger
      data-slot="tool-fallback-trigger"
      aria-label={`${title}${preview ? `（${preview}）` : ""}：${STATUS_ARIA_LABEL[viewStatus]}`}
      className={cn(
        "aui-tool-fallback-trigger group/trigger flex w-full max-w-full cursor-pointer items-center gap-1.5 rounded-md py-0.5 text-left text-sm text-muted-foreground transition-colors",
        "hover:text-secondary-foreground focus-visible:ring-1 focus-visible:ring-ring/50 focus-visible:outline-none",
        className,
      )}
      {...props}
    >
      {/*
        图标位：16px 定宽，动作图标与 chevron 叠在同一格里。
        hover 或展开时图标淡出换成 chevron——与「过程」组头同一套交互语言，
        用户不必学两遍「哪里可以点开」。
      */}
      <span className="relative inline-flex size-4 shrink-0 items-center justify-center">
        <span
          className={cn(
            "absolute inset-0 inline-flex items-center justify-center transition-opacity duration-100",
            "group-hover/trigger:opacity-0 group-data-panel-open/trigger:opacity-0",
          )}
          aria-hidden="true"
        >
          <Icon
            data-slot="tool-fallback-trigger-icon"
            className={cn(
              "aui-tool-fallback-trigger-icon size-3.5",
              isCancelled && "text-muted-foreground/70",
              isInterrupted && "text-amber-600 dark:text-amber-400",
              isFailed && "text-destructive",
              isDenied && "text-amber-600 dark:text-amber-400",
              isRunning && "animate-spin [animation-duration:0.6s]",
            )}
          />
        </span>
        <span
          className={cn(
            "absolute inset-0 inline-flex items-center justify-center opacity-0 transition-opacity duration-100",
            "group-hover/trigger:opacity-100 group-data-panel-open/trigger:opacity-100",
          )}
          aria-hidden="true"
        >
          <ChevronDownIcon className="size-3.5 transition-transform duration-150 group-data-panel-open/trigger:rotate-180" />
        </span>
      </span>
      {/*
        标题：名词短语（运行命令 / 读取文件 / 网页搜索），与组头的叙述式文案
        （执行了命令 / 已读取文件）分工不同——组头讲「这一轮干了什么」，
        这一行讲「这是一次什么调用」。
      */}
      <span
        data-slot="tool-fallback-trigger-label"
        className={cn(
          "aui-tool-fallback-trigger-label shrink-0 leading-none",
          isCancelled && "text-muted-foreground/70 line-through",
          isInterrupted && "text-amber-700 dark:text-amber-400",
          isFailed && "text-destructive",
          isDenied && "text-amber-700 dark:text-amber-400",
          isRunning && "shimmer motion-reduce:animate-none",
        )}
      >
        {title}
      </span>
      {/* 分隔点：2×2 圆角方块。没有摘要时整组隐藏，避免出现「运行命令 · 」 */}
      {preview && (
        <span
          data-slot="tool-fallback-trigger-separator"
          className="inline-block size-0.5 shrink-0 rounded-[1px] bg-muted-foreground/40"
          aria-hidden="true"
        />
      )}
      {/* 折叠时也看得懂在干什么：只展示主参数，完整 args 展开后看 */}
      {preview && (
        <span
          data-slot="tool-fallback-trigger-preview"
          className={cn(
            "aui-tool-fallback-trigger-preview min-w-0 flex-1 truncate text-[13px]",
            // 失败摘要用错误色、被停止用警告色：颜色是这两种终态的**第二重**提示
            // （第一重是行首的状态图标与右侧文案，色弱也读得出）
            isFailed ? "text-destructive" : "",
            (isInterrupted || isDenied) && "text-amber-700 dark:text-amber-400",
            !isFailed && !isInterrupted && !isDenied && "text-muted-foreground/80",
          )}
        >
          {preview}
        </span>
      )}
      {!preview && <span className="min-w-0 flex-1" />}
      {(isInterrupted || isFailed || isDenied) && (
        <span
          data-slot="tool-fallback-trigger-status"
          className={cn(
            "aui-tool-fallback-trigger-status shrink-0 text-xs",
            isFailed ? "text-destructive" : "text-amber-700 dark:text-amber-400",
          )}
        >
          {isInterrupted ? "未完成" : isFailed ? "失败" : "被拒绝"}
        </span>
      )}
      <ToolFallbackDuration className="shrink-0" />
    </CollapsibleTrigger>
  );
}

function ToolFallbackContent({
  className,
  children,
  ...props
}: React.ComponentProps<typeof CollapsibleContent>) {
  return (
    <CollapsibleContent
      data-slot="tool-fallback-content"
      className={cn(
        "aui-tool-fallback-content relative text-sm outline-none",
        "data-closed:pointer-events-none",
        className,
      )}
      {...props}
    >
      {/*
        ps-5 = 20px：与标题文字（图标 16 + gap 4）左缘对齐，参数块不会跑到图标列下方。

        内层刻意没有第二套进场动画（此前是 `animate-in fade-in-0 blur-in-[2px]
        slide-in-from-top-1`）：blur 滤镜要在大片区域上逐帧重新栅格化，还要和父级的
        高度过渡抢同一帧，是「点开一顿」的直接成本之一。高度过渡本身已经足够表达
        「这里展开了」。
      */}
      <div className="flex flex-col gap-2 ps-5 pt-1 pb-2">
        {children}
      </div>
    </CollapsibleContent>
  );
}

/**
 * args / result 共用的纯文本块：限高滚动，超长先折叠。
 * 渲染的是文本节点（无 dangerouslySetInnerHTML），限高与折叠是为了模型吐出大对象时
 * 不把消息列表撑破、也不让每次重渲染都处理巨型文本节点。
 */
function ToolFallbackText({ text, className }: { text: string; className?: string }) {
  const [expanded, setExpanded] = useState(false);
  const { preview, truncated } = previewToolText(text);
  return (
    <div className={cn("flex flex-col items-start gap-1", className)}>
      <pre className="bg-muted/50 text-foreground/90 max-h-64 w-full overflow-auto rounded-md p-2.5 text-xs whitespace-pre-wrap">
        {truncated && !expanded ? preview : text}
      </pre>
      {truncated && (
        <button
          type="button"
          onClick={() => setExpanded((value) => !value)}
          aria-expanded={expanded}
          className="aui-tool-fallback-text-toggle text-muted-foreground hover:text-foreground w-fit cursor-pointer text-xs underline underline-offset-2"
        >
          {expanded ? "收起" : `展开全部（共 ${toolTextLength(text)} 字符）`}
        </button>
      )}
    </div>
  );
}

function ToolFallbackArgs({
  argsText,
  className,
  ...props
}: React.ComponentProps<"div"> & {
  argsText?: string;
}) {
  if (!argsText) return null;

  return (
    <div
      data-slot="tool-fallback-args"
      className={cn("aui-tool-fallback-args", className)}
      {...props}
    >
      <p className="aui-tool-fallback-args-header text-muted-foreground text-xs font-medium">
        参数：
      </p>
      <ToolFallbackText text={argsText} className="aui-tool-fallback-args-value mt-1 w-full" />
    </div>
  );
}

function ToolFallbackResult({
  result,
  className,
  ...props
}: React.ComponentProps<"div"> & {
  result?: unknown;
}) {
  // 带缩进的 JSON.stringify 只跟 result 有关，不要跟着无关的父级重渲染一起跑
  const text = useMemo(() => {
    const failure = failedToolResult(result);
    if (failure) return failure.message || "工具没有返回内容";
    return result === undefined ? "" : formatUnknownValue(result, 2);
  }, [result]);
  const failure = failedToolResult(result);

  if (result === undefined) return null;

  if (failure) {
    return (
      <div
        data-slot="tool-fallback-result"
        className={cn("aui-tool-fallback-result", className)}
        {...props}
      >
        <p className="aui-tool-fallback-result-header text-destructive text-xs font-semibold">
          {failure.__toolOutcome === "denied"
            ? "拒绝原因："
            : failure.__toolOutcome === "interrupted"
              ? "已中断："
              : "错误："}
        </p>
        <ToolFallbackText text={text} className="aui-tool-fallback-result-content mt-1 w-full" />
      </div>
    );
  }

  return (
    <div
      data-slot="tool-fallback-result"
      className={cn("aui-tool-fallback-result", className)}
      {...props}
    >
      <p className="aui-tool-fallback-result-header text-muted-foreground text-xs font-medium">
        结果：
      </p>
      <ToolFallbackText text={text} className="aui-tool-fallback-result-content mt-1 w-full" />
    </div>
  );
}

function ToolFallbackError({
  status,
  className,
  ...props
}: React.ComponentProps<"div"> & {
  status?: ToolCallMessagePartStatus;
}) {
  const error = status?.type === "incomplete" ? status.error : undefined;
  const errorText = useMemo(
    () => (error === undefined || error === null ? null : formatUnknownValue(error)),
    [error],
  );

  if (!errorText) return null;

  const headerText =
    status?.type === "incomplete" && status.reason === "cancelled"
      ? "取消原因："
      : "错误：";

  return (
    <div data-slot="tool-fallback-error" className={cn("aui-tool-fallback-error", className)} {...props}>
      <p className="aui-tool-fallback-error-header text-muted-foreground font-semibold">
        {headerText}
      </p>
      <p className="aui-tool-fallback-error-reason text-muted-foreground max-h-64 overflow-auto whitespace-pre-line">
        {errorText}
      </p>
    </div>
  );
}

const ToolFallbackImpl: ToolCallMessagePartComponent = ({
  toolName,
  argsText,
  result,
  status,
}) => {
  const isCancelled =
    status?.type === "incomplete" && status.reason === "cancelled";
  // 行的默认状态**不跟**「对话显示」走：那一档只决定外层「过程」块是否展开。
  // 单个调用始终收成一行（工具名 + 主参数 + 耗时），点开才看参数与结果的全文——
  // 否则「详细」模式会把每轮的 JSON 全铺在页面上，反而没法读回答。
  // 待用户动作的调用是例外：它正等着人点，必须默认展开。
  const [open, setOpen] = useState(status?.type === "requires-action");

  return (
    <ToolFallbackRoot open={open} onOpenChange={setOpen}>
      <ToolFallbackTrigger
        toolName={toolName}
        argsText={argsText}
        status={status}
        result={result}
      />
      <ToolFallbackContent>
        <ToolFallbackError status={status} />
        <ToolFallbackArgs
          argsText={argsText}
          className={cn(isCancelled && "opacity-60")}
        />
        <ToolFallbackResult result={result} />
      </ToolFallbackContent>
    </ToolFallbackRoot>
  );
};

const ToolFallback = memo(
  ToolFallbackImpl,
) as unknown as ToolCallMessagePartComponent & {
  Root: typeof ToolFallbackRoot;
  Trigger: typeof ToolFallbackTrigger;
  Content: typeof ToolFallbackContent;
  Args: typeof ToolFallbackArgs;
  Result: typeof ToolFallbackResult;
  Error: typeof ToolFallbackError;
};

ToolFallback.displayName = "ToolFallback";
ToolFallback.Root = ToolFallbackRoot;
ToolFallback.Trigger = ToolFallbackTrigger;
ToolFallback.Content = ToolFallbackContent;
ToolFallback.Args = ToolFallbackArgs;
ToolFallback.Result = ToolFallbackResult;
ToolFallback.Error = ToolFallbackError;

export {
  ToolFallback,
  ToolFallbackRoot,
  ToolFallbackTrigger,
  ToolFallbackContent,
  ToolFallbackArgs,
  ToolFallbackResult,
  ToolFallbackError,
};
