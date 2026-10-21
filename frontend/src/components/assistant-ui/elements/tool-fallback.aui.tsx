"use client";

import { memo, useCallback, useMemo, useRef, useState } from "react";
import {
  AlertCircleIcon,
  BanIcon,
  CheckIcon,
  ChevronDownIcon,
  CircleMinusIcon,
  LoaderIcon,
  XCircleIcon,
} from "lucide-react";
import {
  useScrollLock,
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
  type ToolCallViewStatus,
} from "@/lib/tool-call-view";

const ANIMATION_DURATION = 200;

export type ToolFallbackRootProps = Omit<
  React.ComponentProps<typeof Collapsible>,
  "open" | "onOpenChange"
> & {
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  defaultOpen?: boolean;
};

function ToolFallbackRoot({
  className,
  open: controlledOpen,
  onOpenChange: controlledOnOpenChange,
  defaultOpen = false,
  children,
  ...props
}: ToolFallbackRootProps) {
  const collapsibleRef = useRef<HTMLDivElement>(null);
  const [uncontrolledOpen, setUncontrolledOpen] = useState(defaultOpen);
  const lockScroll = useScrollLock(collapsibleRef, ANIMATION_DURATION);

  const isControlled = controlledOpen !== undefined;
  const isOpen = isControlled ? controlledOpen : uncontrolledOpen;

  const handleOpenChange = useCallback(
    (open: boolean) => {
      lockScroll();
      if (!isControlled) {
        setUncontrolledOpen(open);
      }
      controlledOnOpenChange?.(open);
    },
    [lockScroll, isControlled, controlledOnOpenChange],
  );

  return (
    <Collapsible
      ref={collapsibleRef}
      data-slot="tool-fallback-root"
      open={isOpen}
      onOpenChange={handleOpenChange}
      className={cn(
        "aui-tool-fallback-root group/tool-fallback-root w-full",
        className,
      )}
      style={
        {
          "--animation-duration": `${ANIMATION_DURATION}ms`,
        } as React.CSSProperties
      }
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

  const Icon = statusIconMap[viewStatus];

  return (
    <CollapsibleTrigger
      data-slot="tool-fallback-trigger"
      aria-label={`${toolName}${preview ? `（${preview}）` : ""}：${STATUS_ARIA_LABEL[viewStatus]}`}
      className={cn(
        "aui-tool-fallback-trigger group/trigger text-muted-foreground hover:text-foreground flex w-fit origin-left items-center gap-2 py-1.5 text-sm transition-[color,scale] active:scale-[0.98]",
        className,
      )}
      {...props}
    >
      <Icon
        data-slot="tool-fallback-trigger-icon"
        className={cn(
          "aui-tool-fallback-trigger-icon size-4 shrink-0",
          isCancelled && "text-muted-foreground",
          isInterrupted && "text-amber-600 dark:text-amber-400",
          isFailed && "text-destructive",
          isDenied && "text-amber-600 dark:text-amber-400",
          isRunning && "animate-spin [animation-duration:0.6s]",
        )}
      />
      <span
        data-slot="tool-fallback-trigger-label"
        className={cn(
          "aui-tool-fallback-trigger-label-wrapper inline-block text-start leading-none",
          isCancelled && "text-muted-foreground line-through",
          isInterrupted && "text-amber-700 dark:text-amber-400",
          isFailed && "text-destructive",
          isDenied && "text-amber-700 dark:text-amber-400",
          isRunning && "shimmer motion-reduce:animate-none",
        )}
      >
        <b>{toolName}</b>
      </span>
      {/* 折叠时也看得懂在干什么：只展示主参数，完整 args 展开后看 */}
      {preview && (
        <span
          data-slot="tool-fallback-trigger-preview"
          className="aui-tool-fallback-trigger-preview max-w-[45vw] truncate text-xs text-muted-foreground/70 sm:max-w-xs"
        >
          {preview}
        </span>
      )}
      {(isInterrupted || isFailed || isDenied) && (
        <span
          data-slot="tool-fallback-trigger-status"
          className={cn(
            "aui-tool-fallback-trigger-status text-xs",
            isFailed ? "text-destructive" : "text-amber-700 dark:text-amber-400",
          )}
        >
          {isInterrupted ? "未完成" : isFailed ? "失败" : "被拒绝"}
        </span>
      )}
      <ToolFallbackDuration />
      <ChevronDownIcon
        data-slot="tool-fallback-trigger-chevron"
        className={cn(
          "aui-tool-fallback-trigger-chevron size-4 shrink-0",
          "transition-transform duration-(--animation-duration) ease-[cubic-bezier(0.32,0.72,0,1)] motion-reduce:transition-none",
          "-rotate-90",
          "group-data-open/trigger:rotate-0",
          "group-data-panel-open/trigger:rotate-0",
        )}
      />
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
        "aui-tool-fallback-content relative overflow-hidden text-sm outline-none",
        "group/collapsible-content ease-[cubic-bezier(0.32,0.72,0,1)] motion-reduce:animate-none",
        "data-closed:animate-collapsible-up",
        "data-open:animate-collapsible-down",
        "data-closed:fill-mode-forwards",
        "data-closed:pointer-events-none",
        "[--tw-duration:var(--animation-duration)]",
        className,
      )}
      {...props}
    >
      <div
        className={cn(
          "flex flex-col gap-2 ps-6 pt-1 pb-2 ease-[cubic-bezier(0.32,0.72,0,1)] motion-reduce:animate-none",
          "group-data-open/collapsible-content:animate-in group-data-open/collapsible-content:fade-in-0 group-data-open/collapsible-content:blur-in-[2px] group-data-open/collapsible-content:slide-in-from-top-1",
          "group-data-closed/collapsible-content:animate-out group-data-closed/collapsible-content:fade-out-0 group-data-closed/collapsible-content:blur-out-[2px] group-data-closed/collapsible-content:slide-out-to-top-1",
          "group-data-closed/collapsible-content:animation-duration-(--animation-duration) group-data-open/collapsible-content:animation-duration-(--animation-duration)",
        )}
      >
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
          {expanded ? "收起" : `展开全部（共 ${text.length} 字符）`}
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
      <ToolFallbackText text={argsText} className="aui-tool-fallback-args-value" />
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
            ? "Denied reason:"
            : failure.__toolOutcome === "interrupted"
              ? "Interrupted:"
              : "Error:"}
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
        Result:
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
      ? "Cancelled reason:"
      : "Error:";

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
